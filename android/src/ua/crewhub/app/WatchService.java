package ua.crewhub.app;

import android.app.AlarmManager;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.URL;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Фонове стеження за робочим Wi-Fi, навіть коли застосунок закрито.
 * Телефон підключився до Wi-Fi → пінг на сервер → сервер сам починає зміну, якщо це робоча мережа і робочий час.
 * Поки йде зміна — пінг щохвилини, поки телефон не спить; у сні будильник будить службу раз на 5–15 хв
 * (процесор не тримаємо увімкненим — бережемо батарею). Вихід з Wi-Fi служба помічає сама (onLost) і повідомляє
 * точний час (away_since), тож облік не залежить від частоти пінгів.
 * Поза зміною сервер підказує, коли наступна перевірка має сенс (next_check): телефон не будиться даремно.
 */
public class WatchService extends Service {
    static final String CHANNEL = "watch";
    static final int NOTIF_ID = 7;
    static final long PERIOD_MS = 60_000;

    private Handler handler;
    private ConnectivityManager cm;
    private ConnectivityManager.NetworkCallback callback;
    private PowerManager.WakeLock wakeLock;   // лише на час одного запиту (секунди)
    private PowerManager.WakeLock waitLock;   // прийшов раніше: не спимо до початку робочого часу (до ~35 хв)
    private volatile Network wifi;
    private volatile boolean busy;
    private volatile boolean onShift;   // остання відповідь сервера: зміна відкрита
    private volatile Network verified;  // мережа, в якій сервер уже підтвердив свій server_id
    private int failures;               // скільки разів поспіль сервер не відповів (пауза росте)
    private long searchedAt;            // коли востаннє шукали сервер у мережі (не частіше раз на 10 хв)

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("crewhub", Context.MODE_PRIVATE);
    }

    /** Модель і назва телефона для сервера (блок «Пристрої в мережі» і список телефонів людини):
     *  « CrewHubModel/samsung%20SM-S911B CrewHubName/Galaxy%20S23%20Артема». URL-кодовано, бо в заголовку лише ASCII. */
    static String deviceInfo(Context c) {
        StringBuilder b = new StringBuilder(" CrewHubModel/").append(enc((Build.MANUFACTURER + " " + Build.MODEL).trim()));
        String name = null;
        try {
            name = android.provider.Settings.Global.getString(c.getContentResolver(), "device_name");  // «Ім'я пристрою» з налаштувань
            if (name == null || name.trim().isEmpty())
                name = android.provider.Settings.System.getString(c.getContentResolver(), "device_name");  // старі Samsung
        } catch (Exception ignored) {
        }
        if (name != null && !name.trim().isEmpty()) b.append(" CrewHubName/").append(enc(name.trim()));
        return b.toString();
    }

    static String enc(String s) {
        try {
            return java.net.URLEncoder.encode(s, "UTF-8").replace("+", "%20");
        } catch (java.io.UnsupportedEncodingException e) {
            return "";
        }
    }

    /** Постійний id телефона (випадковий, зберігається в налаштуваннях застосунку; не IMEI і не номер). */
    static String deviceId(Context c) {
        SharedPreferences p = prefs(c);
        String id = p.getString("device_id", null);
        if (id == null) {
            id = java.util.UUID.randomUUID().toString().replace("-", "").substring(0, 20);
            p.edit().putString("device_id", id).apply();
        }
        return id;
    }

    static boolean enabled(Context c) {
        SharedPreferences p = prefs(c);
        return p.getString("server", null) != null && p.getBoolean("watch", true);
    }

    static void start(Context c) {
        if (!enabled(c)) return;
        Intent i = new Intent(c, WatchService.class);
        try {
            if (Build.VERSION.SDK_INT >= 26) c.startForegroundService(i);
            else c.startService(i);
        } catch (Exception ignored) {
            // Android 12+ не дає запустити фонову службу без винятку з економії батареї — запуститься, коли відкриють застосунок
        }
    }

    static void stop(Context c) {
        c.stopService(new Intent(c, WatchService.class));
    }

    @Override
    public void onCreate() {
        super.onCreate();
        handler = new Handler(Looper.getMainLooper());
        if (Build.VERSION.SDK_INT >= 26) {
            NotificationChannel ch = new NotificationChannel(CHANNEL, "Робочий Wi-Fi", NotificationManager.IMPORTANCE_LOW);
            ch.setDescription("Автоматичний початок і кінець зміни");
            ch.setShowBadge(false);
            ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).createNotificationChannel(ch);
        }
        if (Build.VERSION.SDK_INT >= 34)
            startForeground(NOTIF_ID, notification("Стежу за робочим Wi-Fi"), android.content.pm.ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE);
        else startForeground(NOTIF_ID, notification("Стежу за робочим Wi-Fi"));
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "crewhub:ping");
        wakeLock.setReferenceCounted(false);
        waitLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "crewhub:start");
        waitLock.setReferenceCounted(false);

        cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        callback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network n) {
                wifi = n;
                failures = 0;
                handler.removeCallbacks(tick);
                handler.postDelayed(tick, 1500); // даємо мережі видати IP
            }

            @Override
            public void onLost(Network n) {
                if (n.equals(wifi)) {
                    wifi = null;
                    if (onShift) leftWifi(n);
                    handler.post(new Runnable() {
                        public void run() { offWifi(); }
                    });
                }
            }
        };
        cm.registerNetworkCallback(new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI).build(), callback);
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        // Сюди ж приходить будильник: у глибокому сні Android (Doze) звичайні таймери стоять, а він — ні.
        // Не спимо лише до кінця цього пінгу (його потік відпустить блокування)
        wakeLock.acquire(30_000L);
        handler.removeCallbacks(tick);
        handler.post(tick);
        return START_STICKY;
    }

    /** Резервний будильник на випадок сну телефона: розбудить службу і пінг піде, навіть якщо телефон ніхто не чіпає. */
    private void scheduleAlarm(long delayMs) {
        try {
            AlarmManager am = (AlarmManager) getSystemService(ALARM_SERVICE);
            PendingIntent pi = PendingIntent.getService(this, 1, new Intent(this, WatchService.class),
                    PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT);
            long at = SystemClock.elapsedRealtime() + Math.max(delayMs, 60_000L);
            if (Build.VERSION.SDK_INT >= 23) am.setAndAllowWhileIdle(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
            else am.set(AlarmManager.ELAPSED_REALTIME_WAKEUP, at, pi);
        } catch (Exception ignored) {
        }
    }

    /** Wi-Fi зник під час зміни: запам'ятовуємо мить і пробуємо одразу сказати серверу (через мобільний інтернет). */
    private void leftWifi(final Network lost) {
        final long since = System.currentTimeMillis() / 1000;
        prefs(this).edit().putLong("away_since", since).apply();
        final PowerManager.WakeLock leftLock = ((PowerManager) getSystemService(POWER_SERVICE))
                .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "crewhub:left");
        leftLock.acquire(60_000L);  // до хвилини: дочекатися мобільного інтернету й відправити звіт
        new Thread(new Runnable() {
            public void run() {
                try {
                    // Мобільний інтернет вмикається не одразу після вимкнення Wi-Fi: чекаємо до 30 с
                    Network other = null;
                    for (int i = 0; i < 10 && other == null; i++) {
                        other = cm.getActiveNetwork();
                        if (other == null || other.equals(lost)) {
                            other = null;
                            Thread.sleep(3000);
                        }
                    }
                    if (other == null) return;
                    if (checkServer(WatchService.this, other, prefs(WatchService.this).getString("server", null)) != SERVER_OK) return;
                    HttpURLConnection c = open(other, "api/clock/left");
                    if (c == null) return;
                    OutputStream out = c.getOutputStream();
                    out.write(("{\"device_id\":\"app-" + deviceId(WatchService.this) + "\",\"away_since\":" + since + "}").getBytes("UTF-8"));
                    out.close();
                    if (c.getResponseCode() == 200) prefs(WatchService.this).edit().remove("away_since").apply();
                    c.disconnect();
                } catch (Exception ignored) {
                    // мобільного інтернету немає — сервер дізнається з наступного пінгу через Wi-Fi (away_since)
                } finally {
                    if (leftLock.isHeld()) leftLock.release();
                }
            }
        }).start();
    }

    static final int SERVER_OK = 1, SERVER_DOWN = 0, SERVER_OTHER = -1;

    /**
     * Чи за адресою сервера відповідає саме наш CrewHub. Cookie входу віддаємо лише після цього:
     * у чужій Wi-Fi (вдома, в кафе) за тим самим 192.168.x.x може стояти будь-який пристрій.
     * Перший успішний id запам'ятовується (або приходить з QR), далі має збігатися. n == null — будь-яка мережа.
     */
    static int checkServer(Context ctx, Network n, String server) {
        if (server == null) return SERVER_DOWN;
        HttpURLConnection c = null;
        try {
            URL url = new URL(server + "api/hello");
            c = (HttpURLConnection) (n != null ? n.openConnection(url) : url.openConnection());
            c.setConnectTimeout(5_000);
            c.setReadTimeout(5_000);
            c.setUseCaches(false);
            // Не 200 (сервер перезапускається, стара версія без /api/hello, тунель перепідключається) —
            // «недоступний»: пробуємо пізніше і cookie, звісно, не віддаємо. «Чужий» — лише якщо id інший
            if (c.getResponseCode() != 200) return SERVER_DOWN;
            String id = new JSONObject(read(c.getInputStream())).optString("server_id", "");
            if (id.isEmpty()) return SERVER_OTHER;
            SharedPreferences p = prefs(ctx);
            String known = p.getString("server_id", null);
            if (known == null) {
                p.edit().putString("server_id", id).apply();
                return SERVER_OK;
            }
            return known.equals(id) ? SERVER_OK : SERVER_OTHER;
        } catch (org.json.JSONException e) {
            return SERVER_OTHER;  // відповідає щось, але не CrewHub
        } catch (Exception e) {
            return SERVER_DOWN;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    private HttpURLConnection open(Network n, String path) throws java.io.IOException {
        String server = prefs(this).getString("server", null);
        if (server == null) return null;
        HttpURLConnection c = (HttpURLConnection) n.openConnection(new URL(server + path));
        c.setConnectTimeout(10_000);
        c.setReadTimeout(15_000);
        c.setRequestMethod("POST");
        c.setDoOutput(true);
        c.setRequestProperty("Content-Type", "application/json");
        c.setRequestProperty("User-Agent", "Mozilla/5.0 (Linux; Android " + Build.VERSION.RELEASE + "; " + Build.MODEL
                + ") CrewHubApp/2.1 background CrewHubDevice/" + deviceId(this) + deviceInfo(this));
        String cookie = prefs(this).getString("cookie", null);
        if (cookie != null) c.setRequestProperty("Cookie", cookie);
        return c;
    }

    private final Runnable tick = new Runnable() {
        public void run() {
            handler.removeCallbacks(this);
            final Network n = wifi;
            if (n == null) {
                offWifi();
                return;
            }
            if (busy) return;
            busy = true;
            wakeLock.acquire(30_000L);  // процесор не засне посеред запиту; далі — знову сон
            new Thread(new Runnable() {
                public void run() {
                    long next = PERIOD_MS;
                    try {
                        next = ping(n);
                    } finally {
                        busy = false;
                        final long delay = next;
                        handler.postDelayed(tick, delay);
                        scheduleAlarm(onShift ? 5 * 60_000L : delay);  // запасний шлях, якщо телефон засне
                        if (wakeLock.isHeld()) wakeLock.release();
                    }
                }
            }).start();
        }
    };

    private void offWifi() {
        handler.removeCallbacks(tick);
        if (wakeLock.isHeld()) wakeLock.release();
        if (waitLock.isHeld()) waitLock.release();
        update("Поза Wi-Fi. Зміна почнеться сама, коли підключитесь до робочої мережі");
    }

    /** Один пінг через саме Wi-Fi (не мобільний інтернет). Повертає, через скільки мс пінгувати знову. */
    private long ping(Network n) {
        SharedPreferences p = prefs(this);
        String server = p.getString("server", null);
        if (server == null) return PERIOD_MS * 5;
        if (!n.equals(verified)) {
            int check = checkServer(this, n, server);
            // Роутер видав серверу інший IP: за старою адресою тиша або чужий пристрій. Шукаємо сервер у своїй
            // підмережі (не з першої ж невдачі — сервер міг просто перезапускатися) і переходимо на нову адресу.
            if (check != SERVER_OK && (check == SERVER_OTHER || failures >= 1)
                    && SystemClock.elapsedRealtime() - searchedAt > 10 * 60_000L) {
                searchedAt = SystemClock.elapsedRealtime();
                String found = discover(this, n, server);
                if (found != null) {
                    moveServer(this, found);
                    server = found;
                    check = SERVER_OK;
                }
            }
            if (check == SERVER_OTHER) {
                if (waitLock.isHeld()) waitLock.release();
                update("Цей Wi-Fi не робочий. Стежу далі");
                return PERIOD_MS * 30;  // інша мережа з іншим пристроєм за цією адресою: до зміни мережі не турбуємо
            }
            if (check == SERVER_DOWN) return offline();
            verified = n;
        }
        HttpURLConnection c = null;
        try {
            c = open(n, "api/clock/auto");
            OutputStream out = c.getOutputStream();
            long away = p.getLong("away_since", 0);
            // unrestricted: чи дозволено роботу без обмежень батареї (сервер показує це в картці зміни)
            boolean unrestricted = ((PowerManager) getSystemService(POWER_SERVICE)).isIgnoringBatteryOptimizations(getPackageName());
            out.write(("{\"device_id\":\"app-" + deviceId(this) + "\",\"unrestricted\":" + unrestricted
                    + (away > 0 ? ",\"away_since\":" + away : "") + "}").getBytes("UTF-8"));
            out.close();
            int code = c.getResponseCode();
            failures = 0;
            saveSessionCookie(p, c.getHeaderFields());
            if (code == 401) {
                if (waitLock.isHeld()) waitLock.release();
                update("Увійдіть у CrewHub, щоб зміна починалась сама");
                return PERIOD_MS * 10;
            }
            JSONObject res = new JSONObject(read(c.getInputStream()));
            if (away > 0) p.edit().remove("away_since").apply();  // сервер отримав, коли саме був вихід
            JSONObject shift = res.optJSONObject("shift");
            String place = res.optString("location", "");
            if (res.isNull("location")) place = "";
            onShift = shift != null;
            if (shift != null) {
                if (waitLock.isHeld()) waitLock.release();  // зміна почалась — далі звичайний сон між пінгами
                boolean paused = "paused".equals(shift.optString("status"));
                String since = time(shift.optLong("started_at"));
                update((paused ? "Перерва · зміна з " : "На зміні з ") + since + (place.isEmpty() ? "" : " · " + place));
                return PERIOD_MS;
            }
            if (!res.isNull("waiting_until") && res.has("waiting_until")) {
                long wait = res.optLong("waiting_until") * 1000 - System.currentTimeMillis();
                // прийшли раніше: не спимо до початку (сервер рахує зміну з початку, якщо ми на місці вчасно)
                waitLock.acquire(Math.min(Math.max(wait, 0) + 2 * 60_000L, 40 * 60_000L));
                update("Ви на місці. Зміна почнеться сама о " + time(res.optLong("waiting_until")));
                return Math.max(Math.min(wait + 5_000, PERIOD_MS * 5), 5_000);
            }
            if (waitLock.isHeld()) waitLock.release();
            // Сервер підказує, коли знову є сенс питати (до початку робочого часу); зміну мережі помітимо й так
            long hint = res.optLong("next_check", 0) * 1000;
            long next = hint > 0 ? Math.min(Math.max(hint, PERIOD_MS), 60 * PERIOD_MS) : 0;
            if (res.optBoolean("other_device")) {
                update("Автозміна вмикається лише з телефона, прив'язаного до вас. Попросіть адміністратора");
                return next > 0 ? next : PERIOD_MS * 10;
            }
            update(res.optBoolean("on_work_network") ? "У робочому Wi-Fi" + (place.isEmpty() ? "" : " · " + place) + ". Зміни зараз немає"
                    : "Цей Wi-Fi не робочий. Стежу далі");
            if (next > 0) return next;
            return res.optBoolean("on_work_network") ? PERIOD_MS * 2 : PERIOD_MS * 5;  // старий сервер без підказки
        } catch (Exception e) {
            return offline();
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /** Сервер не відповідає: під час зміни пробуємо щохвилини, інакше пауза росте 1, 2, 4, 8… до 30 хв. */
    private long offline() {
        failures++;
        update("Немає зв'язку з сервером, пробую ще раз");
        if (onShift) return PERIOD_MS;
        return PERIOD_MS * Math.min(1L << Math.min(failures - 1, 5), 30L);
    }

    /** Flask оновлює session і crewhub_remember у відповідях — зберігаємо свіжі (обидва), щоб вхід не «протухав». */
    static void saveSessionCookie(SharedPreferences p, Map<String, List<String>> headers) {
        if (headers == null) return;
        for (Map.Entry<String, List<String>> h : headers.entrySet()) {
            if (h.getKey() == null || !h.getKey().equalsIgnoreCase("Set-Cookie")) continue;
            for (String v : h.getValue()) {
                String pair = v.split(";", 2)[0].trim();
                int eq = pair.indexOf('=');
                if (eq <= 0 || pair.length() <= eq + 1) continue;
                String name = pair.substring(0, eq);
                if (!name.equals("session") && !name.equals("crewhub_remember")) continue;
                p.edit().putString("cookie", mergeCookie(p.getString("cookie", ""), name, pair)).apply();
                if (name.equals("crewhub_remember")) keepRemember(p, rememberFrom(pair));
            }
        }
    }

    // ── Сервер змінив IP: вхід без пароля ────────────────────────────────────────────────────────────
    // Cookie браузера прив'язані до адреси, тож на новому IP сервера WebView «забуває» вхід. Застосунок тримає
    // токен «запам'ятати мене» окремо, разом з server_id сервера, що його видав, і віддає його лише серверу,
    // який довів, що знає цей токен (proof у /api/hello). Тоді на новій адресі людина входить сама.

    /** Токен «запам'ятати мене» з рядка cookie («a=b; crewhub_remember=selector:validator») або null. */
    static String rememberFrom(String cookies) {
        if (cookies == null) return null;
        for (String part : cookies.split(";")) {
            String t = part.trim();
            if (!t.startsWith("crewhub_remember=")) continue;
            String v = t.substring("crewhub_remember=".length()).replace("\"", "");
            return v.matches("[\\w-]{8,64}:[\\w-]{16,128}") ? v : null;
        }
        return null;
    }

    /** Запам'ятовує токен входу разом з id сервера, який його видав (id вже перевірено checkServer). */
    static void keepRemember(SharedPreferences p, String token) {
        String sid = p.getString("server_id", null);
        if (token == null || sid == null) return;
        if (token.equals(p.getString("remember", null)) && sid.equals(p.getString("remember_sid", null))) return;
        p.edit().putString("remember", token).putString("remember_sid", sid).apply();
    }

    /** Чи за адресою той самий сервер, що видав наш токен: server_id збігається і proof = HMAC-SHA256(sha256(validator), «n:server_id»).
     *  Підставний пристрій з тим самим server_id (його видно в QR) proof не порахує: хеш токена є лише в базі сервера. */
    static boolean proven(Context ctx, Network n, String server, int timeoutMs) {
        SharedPreferences p = prefs(ctx);
        String token = p.getString("remember", null), sid = p.getString("remember_sid", null);
        if (token == null || sid == null || server == null) return false;
        HttpURLConnection c = null;
        try {
            byte[] r = new byte[16];
            new SecureRandom().nextBytes(r);
            String nonce = hex(r);
            int colon = token.indexOf(':');
            URL url = new URL(server + "api/hello?n=" + nonce + "&s=" + token.substring(0, colon));
            c = (HttpURLConnection) (n != null ? n.openConnection(url) : url.openConnection());
            c.setConnectTimeout(timeoutMs);
            c.setReadTimeout(timeoutMs * 2);
            c.setUseCaches(false);
            if (c.getResponseCode() != 200) return false;
            JSONObject o = new JSONObject(read(c.getInputStream()));
            if (!sid.equals(o.optString("server_id"))) return false;
            String key = hex(MessageDigest.getInstance("SHA-256").digest(token.substring(colon + 1).getBytes("UTF-8")));
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key.getBytes("UTF-8"), "HmacSHA256"));
            String want = hex(mac.doFinal((nonce + ":" + sid).getBytes("UTF-8")));
            return MessageDigest.isEqual(want.getBytes("UTF-8"), o.optString("proof", "").getBytes("UTF-8"));
        } catch (Exception e) {
            return false;
        } finally {
            if (c != null) c.disconnect();
        }
    }

    /**
     * Сервер не відповідає за старою адресою: шукаємо його в підмережі телефона (/24, той самий порт) за доказом proof.
     * Лише якщо стара адреса — IP у тій самій /24, що й телефон (ми в мережі роботи, а не вдома) і є токен входу.
     * До 254 запитів по 48 паралельно, ~3–6 с. Повертає нову адресу сервера або null. Не з UI-потоку.
     */
    static String discover(final Context ctx, final Network n, String server) {
        SharedPreferences p = prefs(ctx);
        if (server == null || p.getString("remember", null) == null) return null;
        final URL old;
        try {
            old = new URL(server);
        } catch (Exception e) {
            return null;
        }
        if (!old.getHost().matches("\\d{1,3}(\\.\\d{1,3}){3}")) return null;  // тунель чи доменне ім'я: там IP не «стрибає»
        ConnectivityManager cm = (ConnectivityManager) ctx.getSystemService(CONNECTIVITY_SERVICE);
        Network net = n != null ? n : cm.getActiveNetwork();
        LinkProperties lp = net == null ? null : cm.getLinkProperties(net);
        if (lp == null) return null;
        String mine = null;
        for (LinkAddress a : lp.getLinkAddresses()) {
            InetAddress ia = a.getAddress();
            if (ia instanceof Inet4Address && ia.isSiteLocalAddress()) mine = ia.getHostAddress();
        }
        if (mine == null) return null;
        final String prefix = mine.substring(0, mine.lastIndexOf('.') + 1);
        if (!old.getHost().startsWith(prefix)) return null;  // інша мережа (вдома): нічого не скануємо
        List<Callable<String>> jobs = new ArrayList<>();
        for (int i = 1; i < 255; i++) {
            final String cand = old.getProtocol() + "://" + prefix + i + (old.getPort() > 0 ? ":" + old.getPort() : "") + old.getPath();
            if (cand.equals(server) || (prefix + i).equals(mine)) continue;
            jobs.add(new Callable<String>() {
                public String call() throws Exception {
                    if (proven(ctx, n, cand, 800)) return cand;
                    throw new Exception("not here");
                }
            });
        }
        ExecutorService pool = Executors.newFixedThreadPool(48);
        try {
            return pool.invokeAny(jobs, 20, TimeUnit.SECONDS);
        } catch (Exception e) {
            return null;  // ніхто не довів, що він наш сервер
        } finally {
            pool.shutdownNow();
        }
    }

    /** Знайшли сервер на новій адресі: запам'ятовуємо її, а для фонових пінгів — вхід токеном. */
    static void moveServer(Context ctx, String found) {
        SharedPreferences p = prefs(ctx);
        p.edit().putString("server", found).putString("server_id", p.getString("remember_sid", null))
                .putString("cookie", "crewhub_remember=" + p.getString("remember", "")).apply();
    }

    static String hex(byte[] b) {
        StringBuilder sb = new StringBuilder();
        for (byte x : b) sb.append(String.format(Locale.ROOT, "%02x", x));
        return sb.toString();
    }

    /** Замінює cookie з іменем name у рядку cur на pair (або додає, якщо такого ще немає). */
    static String mergeCookie(String cur, String name, String pair) {
        StringBuilder out = new StringBuilder();
        boolean replaced = false;
        if (cur != null) for (String part : cur.split(";")) {
            String t = part.trim();
            if (t.isEmpty()) continue;
            if (t.startsWith(name + "=")) { t = pair; replaced = true; }
            if (out.length() > 0) out.append("; ");
            out.append(t);
        }
        if (!replaced) { if (out.length() > 0) out.append("; "); out.append(pair); }
        return out.toString();
    }

    private static String read(InputStream in) throws java.io.IOException {
        ByteArrayOutputStream b = new ByteArrayOutputStream();
        byte[] buf = new byte[4096];
        int r;
        while ((r = in.read(buf)) > 0) b.write(buf, 0, r);
        in.close();
        return b.toString("UTF-8");
    }

    private static String time(long sec) {
        return new SimpleDateFormat("HH:mm", Locale.getDefault()).format(new Date(sec * 1000));
    }

    private String lastText;

    private void update(String text) {
        if (text.equals(lastText)) return;
        lastText = text;
        ((NotificationManager) getSystemService(NOTIFICATION_SERVICE)).notify(NOTIF_ID, notification(text));
    }

    @SuppressWarnings("deprecation")
    private Notification notification(String text) {
        Notification.Builder b = Build.VERSION.SDK_INT >= 26 ? new Notification.Builder(this, CHANNEL) : new Notification.Builder(this);
        Intent open = new Intent(this, MainActivity.class).setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        b.setSmallIcon(R.drawable.icon)
                .setContentTitle("CrewHub")
                .setContentText(text)
                .setOngoing(true)
                .setShowWhen(false)
                .setContentIntent(PendingIntent.getActivity(this, 0, open, PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT));
        if (Build.VERSION.SDK_INT < 26) b.setPriority(Notification.PRIORITY_LOW);
        return b.build();
    }

    @Override
    public void onDestroy() {
        handler.removeCallbacksAndMessages(null);
        try {
            cm.unregisterNetworkCallback(callback);
        } catch (Exception ignored) {
        }
        if (wakeLock.isHeld()) wakeLock.release();
        if (waitLock.isHeld()) waitLock.release();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}

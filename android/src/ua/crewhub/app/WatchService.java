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
import java.net.URL;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/**
 * Фонове стеження за робочим Wi-Fi, навіть коли застосунок закрито.
 * Телефон підключився до Wi-Fi → пінг на сервер → сервер сам починає зміну, якщо це робоча мережа і робочий час.
 * Поки йде зміна — пінг щохвилини (сервер бачить, що людина на місці). Пішов з Wi-Fi — пінги припиняються,
 * сервер ставить зміну на паузу і після кінця робочого часу закриває її часом виходу.
 */
public class WatchService extends Service {
    static final String CHANNEL = "watch";
    static final int NOTIF_ID = 7;
    static final long PERIOD_MS = 60_000;

    private Handler handler;
    private ConnectivityManager cm;
    private ConnectivityManager.NetworkCallback callback;
    private PowerManager.WakeLock wakeLock;
    private volatile Network wifi;
    private volatile boolean busy;
    private volatile boolean onShift;   // остання відповідь сервера: зміна відкрита

    static SharedPreferences prefs(Context c) {
        return c.getSharedPreferences("crewhub", Context.MODE_PRIVATE);
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
        wakeLock = ((PowerManager) getSystemService(POWER_SERVICE)).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "crewhub:shift");
        wakeLock.setReferenceCounted(false);

        cm = (ConnectivityManager) getSystemService(CONNECTIVITY_SERVICE);
        callback = new ConnectivityManager.NetworkCallback() {
            @Override
            public void onAvailable(Network n) {
                wifi = n;
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
        // Сюди ж приходить будильник: у глибокому сні Android (Doze) звичайні таймери стоять, а він — ні
        if (!wakeLock.isHeld()) wakeLock.acquire(60_000L);
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
                    HttpURLConnection c = open(other, "api/clock/left");
                    if (c == null) return;
                    OutputStream out = c.getOutputStream();
                    out.write(("{\"device_id\":\"app-" + deviceId(WatchService.this) + "\",\"away_since\":" + since + "}").getBytes("UTF-8"));
                    out.close();
                    if (c.getResponseCode() == 200) prefs(WatchService.this).edit().remove("away_since").apply();
                    c.disconnect();
                } catch (Exception ignored) {
                    // мобільного інтернету немає — сервер дізнається з наступного пінгу через Wi-Fi (away_since)
                }
            }
        }).start();
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
                + ") CrewHubApp/1.9 background CrewHubDevice/" + deviceId(this));
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
                    }
                }
            }).start();
        }
    };

    private void offWifi() {
        handler.removeCallbacks(tick);
        if (wakeLock.isHeld()) wakeLock.release();
        update("Поза Wi-Fi. Зміна почнеться сама, коли підключитесь до робочої мережі");
    }

    /** Один пінг через саме Wi-Fi (не мобільний інтернет). Повертає, через скільки мс пінгувати знову. */
    private long ping(Network n) {
        SharedPreferences p = prefs(this);
        String server = p.getString("server", null);
        if (server == null) return PERIOD_MS * 5;
        HttpURLConnection c = null;
        try {
            c = open(n, "api/clock/auto");
            OutputStream out = c.getOutputStream();
            long away = p.getLong("away_since", 0);
            // unrestricted: без обмежень батареї служба пінгує щохвилини і в сні, тож сервер швидше помічає вимкнений Wi-Fi
            boolean unrestricted = ((PowerManager) getSystemService(POWER_SERVICE)).isIgnoringBatteryOptimizations(getPackageName());
            out.write(("{\"device_id\":\"app-" + deviceId(this) + "\",\"unrestricted\":" + unrestricted
                    + (away > 0 ? ",\"away_since\":" + away : "") + "}").getBytes("UTF-8"));
            out.close();
            int code = c.getResponseCode();
            saveSessionCookie(p, c.getHeaderFields());
            if (code == 401) {
                if (wakeLock.isHeld()) wakeLock.release();
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
                wakeLock.acquire(10 * 60_000L); // поки йде зміна, пінги не засинають; авто-звільнення через 10 хв
                boolean paused = "paused".equals(shift.optString("status"));
                String since = time(shift.optLong("started_at"));
                update((paused ? "Перерва · зміна з " : "На зміні з ") + since + (place.isEmpty() ? "" : " · " + place));
                return PERIOD_MS;
            }
            if (!res.isNull("waiting_until") && res.has("waiting_until")) {
                long wait = res.optLong("waiting_until") * 1000 - System.currentTimeMillis();
                wakeLock.acquire(Math.max(wait, 0) + 3 * 60_000L); // прийшли раніше: прокинутись рівно на початок
                update("Ви на місці. Зміна почнеться сама о " + time(res.optLong("waiting_until")));
                return Math.max(Math.min(wait + 5_000, PERIOD_MS * 5), 5_000);
            }
            if (wakeLock.isHeld()) wakeLock.release();
            if (res.optBoolean("other_device")) {
                update("Автозміна вмикається лише з телефона, прив'язаного до вас. Попросіть адміністратора");
                return PERIOD_MS * 10;
            }
            update(res.optBoolean("on_work_network") ? "У робочому Wi-Fi" + (place.isEmpty() ? "" : " · " + place) + ". Зміни зараз немає"
                    : "Цей Wi-Fi не робочий. Стежу далі");
            return res.optBoolean("on_work_network") ? PERIOD_MS * 2 : PERIOD_MS * 5;
        } catch (Exception e) {
            update("Немає зв'язку з сервером, пробую ще раз");
            return PERIOD_MS;
        } finally {
            if (c != null) c.disconnect();
        }
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
            }
        }
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
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}

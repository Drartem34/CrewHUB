package ua.crewhub.app;

import android.app.Activity;
import android.app.AlertDialog;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.net.wifi.WifiConfiguration;
import android.net.wifi.WifiManager;
import android.net.wifi.WifiNetworkSuggestion;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.provider.Settings;
import android.os.Environment;
import android.text.InputType;
import android.view.Gravity;
import android.view.KeyEvent;
import android.view.View;
import android.view.inputmethod.EditorInfo;
import android.webkit.CookieManager;
import android.webkit.URLUtil;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Locale;

/**
 * CrewHub для Android: вбудований браузер для сервера CrewHub у вашій мережі.
 * Перевірка Wi-Fi відбувається на сервері (він бачить IP телефона), тому застосунку
 * не потрібні дозволи на геолокацію.
 * Підключення — QR-кодом або кодом підключення: працівник бачить назву компанії, а не IP сервера.
 */
public class MainActivity extends Activity {
    private static final String PREFS = "crewhub";
    private static final String KEY_SERVER = "server";
    private volatile long searchedAt;  // коли востаннє шукали сервер у мережі (verifyThenLoad)
    private static final int PICK_FILE = 1;
    private static final int SCAN = 4;
    private static final int ADD_WIFI = 5;
    private static final String CODE_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
    private static final byte[] CODE_KEY = "CrewHub!".getBytes();
    private String pendingServer;  // чекаємо, поки телефон підключиться до Wi-Fi з QR
    private static final int BRAND = Color.parseColor("#E8173D");

    private WebView web;
    private boolean contentShown;  // уже показано екран (налаштування, сторінку чи «Підключаюсь»)
    private ProgressBar progress;
    private ValueCallback<Uri[]> fileCallback;
    private boolean mainFrameFailed;
    private boolean askedPermissions;
    private View loading;          // екран «Підключаюсь…», поки сервер не відповів
    private boolean pageShown;
    private final Runnable loadTimeout = new Runnable() {
        public void run() {
            if (!pageShown && web != null) {
                String server = prefs().getString(KEY_SERVER, "");
                showSetup(server, label(server) + " не відповідає.\nПеревірте, що телефон у робочому Wi-Fi, а сервер запущено.");
            }
        }
    };
    private final Runnable showProgress = new Runnable() {
        public void run() { if (progress != null) progress.setVisibility(View.VISIBLE); }
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().setStatusBarColor(Color.parseColor("#000000"));
        if (handleLink(getIntent())) return;
        String server = prefs().getString(KEY_SERVER, null);
        if (server == null) showSetup(null, null);
        else showWeb(server, state);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        handleLink(intent);
    }

    /** QR відскановано камерою телефона: посилання crewhub://connect?... відкриває застосунок. */
    private boolean handleLink(Intent intent) {
        Uri u = intent == null ? null : intent.getData();
        if (u == null || !"crewhub".equals(u.getScheme())) return false;
        intent.setData(null);
        return confirmLink(u, true);
    }

    /**
     * Посилання з будь-якої програми (месенджер, сайт, камера) могло б тихо перенаправити застосунок на чужий сервер,
     * який попросить номер і пароль. Тому адресу міняємо лише після згоди людини: показуємо назву й адресу.
     * external = false — QR відскановано в самому застосунку: питаємо, лише якщо вже є інший сервер.
     */
    private boolean confirmLink(final Uri u, boolean external) {
        if (!"connect".equals(u.getHost())) return false;
        String raw = fromB64(u.getQueryParameter("s"));
        String server = raw == null ? null : normalizeServer(raw);
        if (server == null) return applyLink(u);  // покаже «QR-код пошкоджено»
        final String current = prefs().getString(KEY_SERVER, null);
        if (server.equals(current) || (!external && current == null)) return applyLink(u);
        if (!contentShown) {
            if (current == null) showSetup(null, null);
            else showWeb(current, null);
        }
        String company = u.getQueryParameter("n");
        new AlertDialog.Builder(this)
                .setTitle("Підключитися до сервера?")
                .setMessage((company != null && !company.isEmpty() ? "«" + company + "»\n" : "")
                        + "Адреса: " + Uri.parse(server).getAuthority()
                        + (current != null ? "\n\nЗараз застосунок підключено до іншого сервера. "
                        + "Погоджуйтесь, лише якщо цей QR-код показав ваш адміністратор." : ""))
                .setPositiveButton("Підключитися", new android.content.DialogInterface.OnClickListener() {
                    public void onClick(android.content.DialogInterface d, int w) { applyLink(u); }
                })
                .setNegativeButton("Скасувати", null)
                .setCancelable(false)
                .show();
        return true;
    }

    /** Назва для людини: компанія замість адреси сервера (адресу бачить лише той, хто ввів її вручну). */
    private String label(String server) {
        String company = prefs().getString("company", "");
        if (!company.isEmpty()) return company;
        if (prefs().getBoolean("hidden", false) || server == null) return "Сервер компанії";
        return "Сервер " + server.replaceFirst("^https?://", "").replaceFirst("/$", "");
    }

    private static String fromB64(String v) {
        if (v == null) return null;
        try {
            return new String(android.util.Base64.decode(v, android.util.Base64.URL_SAFE | android.util.Base64.NO_PADDING | android.util.Base64.NO_WRAP), "UTF-8");
        } catch (Exception e) {
            return null;
        }
    }

    /** crewhub://connect?s=<адреса>&n=<компанія>&w=<Wi-Fi>&p=<пароль> */
    private boolean applyLink(Uri u) {
        if (!"connect".equals(u.getHost())) return false;
        String server = normalizeServer(fromB64(u.getQueryParameter("s")) == null ? "" : fromB64(u.getQueryParameter("s")));
        if (server == null) {
            Toast.makeText(this, "QR-код пошкоджено, попросіть адміністратора показати його ще раз", Toast.LENGTH_LONG).show();
            return false;
        }
        String company = u.getQueryParameter("n");
        String serverId = u.getQueryParameter("i");  // id сервера з QR: застосунок звірятиме його перед входом
        SharedPreferences.Editor e = prefs().edit().putString(KEY_SERVER, server).putBoolean("hidden", true)
                .putString("company", company == null ? "" : company).remove("cookie").remove("server_id");
        if (serverId != null && serverId.matches("[0-9a-f]{8,64}")) e.putString("server_id", serverId);
        e.apply();
        String ssid = fromB64(u.getQueryParameter("w"));
        if (ssid != null && !ssid.isEmpty()) {
            pendingServer = server;
            if (offerWifi(ssid, fromB64(u.getQueryParameter("p")))) {
                showConnecting(server, "Підключаюсь до Wi-Fi «" + ssid + "»");
                return true;
            }
            pendingServer = null;
        }
        showWeb(server, null);
        return true;
    }

    /** Код підключення (K7QM2-D9FXA): перемішані IPv4 і порт сервера. null — це не код. */
    static String decodeCode(String raw) {
        String c = raw.toUpperCase(Locale.ROOT).replaceAll("[\\s-]", "").replace('O', '0').replace('I', '1').replace('L', '1');
        if (c.length() != 10) return null;
        long n = 0;
        for (int i = 0; i < c.length(); i++) {
            int idx = CODE_ALPHABET.indexOf(c.charAt(i));
            if (idx < 0) return null;
            n = (n << 5) | idx;
        }
        n >>= 2;
        int[] b = new int[6];
        for (int i = 5; i >= 0; i--) {
            b[i] = (int) ((n & 0xff) ^ (CODE_KEY[i] & 0xff));
            n >>= 8;
        }
        int port = (b[4] << 8) | b[5];
        if (port == 0) return null;
        return "http://" + b[0] + "." + b[1] + "." + b[2] + "." + b[3] + ":" + port + "/";
    }

    /** Wi-Fi з QR: Android 11+ показує своє вікно «Зберегти мережу?», старіші додають її самі. */
    @SuppressWarnings("deprecation")
    private boolean offerWifi(String ssid, String pass) {
        pass = pass == null ? "" : pass;
        try {
            WifiManager wm = (WifiManager) getApplicationContext().getSystemService(WIFI_SERVICE);
            if (Build.VERSION.SDK_INT >= 29) {
                WifiNetworkSuggestion.Builder b = new WifiNetworkSuggestion.Builder().setSsid(ssid);
                if (!pass.isEmpty()) b.setWpa2Passphrase(pass);
                java.util.ArrayList<WifiNetworkSuggestion> list = new java.util.ArrayList<>();
                list.add(b.build());
                if (Build.VERSION.SDK_INT >= 30) {
                    Intent i = new Intent(Settings.ACTION_WIFI_ADD_NETWORKS);
                    i.putParcelableArrayListExtra(Settings.EXTRA_WIFI_NETWORK_LIST, list);
                    startActivityForResult(i, ADD_WIFI);
                    return true;
                }
                wm.addNetworkSuggestions(list);
                Toast.makeText(this, "Телефон запропонує підключитися до Wi-Fi «" + ssid + "»", Toast.LENGTH_LONG).show();
            } else {
                if (!wm.isWifiEnabled()) wm.setWifiEnabled(true);
                WifiConfiguration conf = new WifiConfiguration();
                conf.SSID = "\"" + ssid + "\"";
                if (pass.isEmpty()) conf.allowedKeyManagement.set(WifiConfiguration.KeyMgmt.NONE);
                else conf.preSharedKey = "\"" + pass + "\"";
                int id = wm.addNetwork(conf);
                if (id != -1) wm.enableNetwork(id, true);
            }
            new android.os.Handler().postDelayed(new Runnable() {
                public void run() { finishWifi(); }
            }, 5000);
            return true;
        } catch (Exception e) {
            Toast.makeText(this, "Підключіться до Wi-Fi «" + ssid + "» вручну", Toast.LENGTH_LONG).show();
            return false;
        }
    }

    private void finishWifi() {
        if (pendingServer == null) return;
        final String server = pendingServer;
        pendingServer = null;
        showWeb(server, null);
    }

    private void showConnecting(String server, String text) {
        contentShown = true;
        View v = loadingView(server);
        ((TextView) v.findViewWithTag("status")).setText(text);
        setContentView(v);
    }

    private SharedPreferences prefs() {
        return getSharedPreferences(PREFS, Context.MODE_PRIVATE);
    }

    /** 192.168.1.23 → http://192.168.1.23:5000/ ; повна адреса (https://…) лишається як є. */
    static String normalizeServer(String raw) {
        String s = raw.trim();
        if (s.isEmpty()) return null;
        if (!s.startsWith("http://") && !s.startsWith("https://")) {
            String host = s.contains("/") ? s.substring(0, s.indexOf('/')) : s;
            if (!host.contains(":") && host.matches("[0-9.]+")) s = host + ":5000" + s.substring(host.length());
            s = "http://" + s;
        }
        if (!s.endsWith("/")) s += "/";
        return URLUtil.isValidUrl(s) ? s : null;
    }

    private boolean night() {
        return (getResources().getConfiguration().uiMode & android.content.res.Configuration.UI_MODE_NIGHT_MASK)
                == android.content.res.Configuration.UI_MODE_NIGHT_YES;
    }

    private int textColor() { return night() ? Color.parseColor("#F2F2F2") : Color.parseColor("#101828"); }

    private int mutedColor() { return night() ? Color.parseColor("#A3A3A3") : Color.parseColor("#475569"); }

    private int bgColor() { return night() ? Color.parseColor("#050505") : Color.parseColor("#F4F5FA"); }

    private int dp(int v) {
        return Math.round(v * getResources().getDisplayMetrics().density);
    }

    // ------------------------------------------------------------------ налаштування сервера
    private void showSetup(String current, String error) {
        contentShown = true;
        if (web != null) {
            web.removeCallbacks(loadTimeout);
            web.destroy();
            web = null;
        }
        ScrollView scroll = new ScrollView(this);
        scroll.setBackgroundColor(bgColor());
        scroll.setFillViewport(true);
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER_VERTICAL);
        box.setPadding(dp(28), dp(40), dp(28), dp(40));
        scroll.addView(box);

        TextView logo = new TextView(this);
        logo.setText("CrewHub");
        logo.setTextSize(30);
        logo.setTypeface(Typeface.DEFAULT_BOLD);
        logo.setTextColor(BRAND);
        box.addView(logo);

        TextView hint = new TextView(this);
        boolean hidden = prefs().getBoolean("hidden", false);
        hint.setText("Облік змін з перевіркою робочого Wi-Fi.\n\n"
                + (current != null ? "Зараз: " + label(current) + "\n\n" : "")
                + "Відскануйте QR-код підключення або введіть код. Їх показує адміністратор у розділі «Точки і Wi-Fi».");
        hint.setTextSize(15);
        hint.setTextColor(mutedColor());
        hint.setPadding(0, dp(8), 0, dp(20));
        box.addView(hint);

        Button scan = new Button(this);
        scan.setText("Сканувати QR-код");
        scan.setTextColor(Color.WHITE);
        scan.setTextSize(17);
        GradientDrawable sbg = new GradientDrawable();
        sbg.setColor(BRAND);
        sbg.setCornerRadius(dp(14));
        scan.setBackground(sbg);
        scan.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) {
                startActivityForResult(new Intent(MainActivity.this, ScanActivity.class), SCAN);
            }
        });
        LinearLayout.LayoutParams slp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(54));
        slp.bottomMargin = dp(18);
        box.addView(scan, slp);

        final EditText input = new EditText(this);
        input.setHint("Код підключення, напр. K7QM2-D9FXA");
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        input.setImeOptions(EditorInfo.IME_ACTION_GO);
        input.setTextSize(18);
        input.setTextColor(textColor());
        input.setHintTextColor(mutedColor());
        if (current != null && !hidden) input.setText(current.replaceFirst("^http://", "").replaceFirst("/$", ""));
        box.addView(input);

        if (error != null) {
            TextView err = new TextView(this);
            err.setText(error);
            err.setTextColor(Color.parseColor("#DC2626"));
            err.setPadding(0, dp(8), 0, 0);
            box.addView(err);
        }

        final android.widget.CheckBox watch = new android.widget.CheckBox(this);
        watch.setText("Автозміна: починати й закінчувати зміну за робочим Wi-Fi, навіть коли застосунок закрито");
        watch.setTextSize(15);
        watch.setTextColor(textColor());
        watch.setChecked(prefs().getBoolean("watch", true));
        watch.setPadding(dp(4), dp(14), 0, 0);
        box.addView(watch);

        Button go = new Button(this);
        go.setText("Підключитися");
        go.setTextColor(BRAND);
        go.setTextSize(17);
        GradientDrawable bg = new GradientDrawable();
        bg.setColor(Color.TRANSPARENT);
        bg.setStroke(dp(2), BRAND);
        bg.setCornerRadius(dp(14));
        go.setBackground(bg);
        LinearLayout.LayoutParams lp = new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(54));
        lp.topMargin = dp(20);
        box.addView(go, lp);

        final Runnable connect = new Runnable() {
            @Override
            public void run() {
                String typed = input.getText().toString();
                String url = decodeCode(typed);
                boolean byCode = url != null;  // код підключення: адресу людина не бачить
                if (url == null && typed.trim().isEmpty() && current != null) url = current;  // лише змінили галочку
                else if (url == null) url = normalizeServer(typed);
                if (url == null) {
                    input.setError("Перевірте код");
                    return;
                }
                // «Підключитися» — свідома дія людини: id сервера запам'ятовується заново (сервер могли перевстановити)
                SharedPreferences.Editor e = prefs().edit().putBoolean("watch", watch.isChecked()).remove("server_id");
                if (!url.equals(current)) {
                    e.putString(KEY_SERVER, url).putBoolean("hidden", byCode).remove("company").remove("cookie");
                }
                e.apply();
                if (!watch.isChecked()) WatchService.stop(MainActivity.this);
                showWeb(url, null);
            }
        };
        go.setOnClickListener(new View.OnClickListener() {
            @Override
            public void onClick(View v) {
                connect.run();
            }
        });
        input.setOnEditorActionListener(new TextView.OnEditorActionListener() {
            @Override
            public boolean onEditorAction(TextView v, int actionId, KeyEvent event) {
                connect.run();
                return true;
            }
        });
        setContentView(scroll);
    }

    // ------------------------------------------------------------------ веб-частина
    private void showWeb(final String server, Bundle state) {
        contentShown = true;
        FrameLayout frame = new FrameLayout(this);
        frame.setBackgroundColor(bgColor());
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        frame.addView(root, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        progress = new ProgressBar(this, null, android.R.attr.progressBarStyleHorizontal);
        progress.setIndeterminate(true);
        root.addView(progress, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, dp(4)));
        progress.setVisibility(View.GONE);
        web = new WebView(this);
        web.resumeTimers();  // таймери могли лишитись на паузі від попереднього WebView (pauseTimers — на весь процес)
        web.setBackgroundColor(Color.TRANSPARENT);          // без білого спалаху між сторінками в темній темі
        web.setOverScrollMode(View.OVER_SCROLL_IF_CONTENT_SCROLLS);
        root.addView(web, new LinearLayout.LayoutParams(LinearLayout.LayoutParams.MATCH_PARENT, 0, 1));
        loading = loadingView(server);
        frame.addView(loading, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        pageShown = false;
        setContentView(frame);
        web.postDelayed(loadTimeout, 15000);  // сервер не відповів за 15 с — не тримаємо порожній екран

        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setDatabaseEnabled(true);
        s.setLoadWithOverviewMode(true);
        s.setUseWideViewPort(true);
        // CrewHubDevice — постійний id цього телефона: сервер запам'ятовує його при реєстрації і вмикає автозміну лише з нього
        s.setUserAgentString(s.getUserAgentString() + " CrewHubApp/2.3 CrewHubDevice/" + WatchService.deviceId(this)
                + WatchService.deviceInfo(this) + (weakPhone() ? " CrewHubLite" : ""));
        CookieManager.getInstance().setAcceptCookie(true);
        CookieManager.getInstance().setAcceptThirdPartyCookies(web, true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            @SuppressWarnings("deprecation") // варіант з WebResourceRequest з'явився лише в Android 7
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                Uri uri = Uri.parse(url);
                if ("crewhub".equals(uri.getScheme())) {           // crewhub://settings зі сторінки входу
                    showSetup(server, null);
                    return true;
                }
                String scheme = uri.getScheme();
                if ("tel".equals(scheme) || "mailto".equals(scheme) || !uri.toString().startsWith(server.replaceFirst("/$", ""))) {
                    try {
                        startActivity(new Intent(Intent.ACTION_VIEW, uri));  // дзвінок, Telegram, зовнішні посилання
                    } catch (Exception ignored) {
                    }
                    return true;
                }
                return false;
            }

            @Override
            public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
                mainFrameFailed = false;
                // Смужку показуємо лише для довгих завантажень: швидкі переходи лишаються плавними, без миготіння
                progress.removeCallbacks(showProgress);
                progress.postDelayed(showProgress, 400);
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (!mainFrameFailed && !pageShown) {
                    pageShown = true;
                    view.removeCallbacks(loadTimeout);
                    if (loading != null) loading.setVisibility(View.GONE);
                }
                progress.removeCallbacks(showProgress);
                progress.setVisibility(View.GONE);
                CookieManager.getInstance().flush();
                // Фонова служба Wi-Fi ходить на сервер з тими ж cookie, що й застосунок:
                // і session, і crewhub_remember (постійний вхід), щоб пінги не «протухали».
                String all = CookieManager.getInstance().getCookie(server);
                if (all != null && all.contains("session=")) prefs().edit().putString("cookie", all.trim()).apply();
                // Токен входу — ще й окремо, з id сервера: переживе зміну адреси сервера (див. WatchService.discover)
                WatchService.keepRemember(prefs(), WatchService.rememberFrom(all));
                // Назва компанії з сторінки сервера: її застосунок показує замість IP
                view.evaluateJavascript("(function(){var m=document.querySelector('meta[name=crewhub-company]');return m?m.content:''})()",
                        new ValueCallback<String>() {
                            public void onReceiveValue(String v) {
                                if (v != null && v.length() > 2) {
                                    try {
                                        String name = new org.json.JSONArray("[" + v + "]").getString(0);
                                        if (!name.isEmpty()) prefs().edit().putString("company", name).apply();
                                    } catch (Exception ignored) {
                                    }
                                }
                            }
                        });
                // Сторінка входу: сервер не прийняв токен (вийшли, змінили пароль, «Вийти на всіх») — забуваємо його
                if (url.contains("/login")) prefs().edit().remove("cookie").remove("remember").remove("remember_sid").apply();
                else if (!askedPermissions && url.startsWith(server.replaceFirst("/$", ""))) {
                    askedPermissions = true;
                    setupBackground();
                }
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame() && !mainFrameFailed) {
                    mainFrameFailed = true;
                    showSetup(server, "Не вдалося відкрити: " + label(server) + "\n" + err.getDescription()
                            + "\nПеревірте, що телефон у робочому Wi-Fi, а сервер запущено.");
                }
            }
        });

        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback, FileChooserParams params) {
                if (fileCallback != null) fileCallback.onReceiveValue(null);
                fileCallback = callback;
                try {
                    startActivityForResult(params.createIntent(), PICK_FILE);
                } catch (Exception e) {
                    fileCallback = null;
                    return false;
                }
                return true;
            }

            @Override
            public boolean onJsConfirm(WebView view, String url, String message, final android.webkit.JsResult result) {
                new AlertDialog.Builder(MainActivity.this)
                        .setMessage(message)
                        .setPositiveButton("Так", new android.content.DialogInterface.OnClickListener() {
                            public void onClick(android.content.DialogInterface d, int w) { result.confirm(); }
                        })
                        .setNegativeButton("Скасувати", new android.content.DialogInterface.OnClickListener() {
                            public void onClick(android.content.DialogInterface d, int w) { result.cancel(); }
                        })
                        .setOnCancelListener(new android.content.DialogInterface.OnCancelListener() {
                            public void onCancel(android.content.DialogInterface d) { result.cancel(); }
                        })
                        .show();
                return true;
            }
        });

        // Табелі CSV: завантаження з тими самими cookie, що й у сесії
        web.setDownloadListener(new android.webkit.DownloadListener() {
          @Override
          public void onDownloadStart(String url, String userAgent, String contentDisposition, String mimeType, long length) {
            try {
                DownloadManager.Request r = new DownloadManager.Request(Uri.parse(url));
                r.addRequestHeader("Cookie", CookieManager.getInstance().getCookie(url));
                r.addRequestHeader("User-Agent", userAgent);
                String name = URLUtil.guessFileName(url, contentDisposition, mimeType);
                r.setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name);
                r.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                ((DownloadManager) getSystemService(DOWNLOAD_SERVICE)).enqueue(r);
                Toast.makeText(MainActivity.this, "Завантажується " + name, Toast.LENGTH_SHORT).show();
            } catch (Exception e) {
                Toast.makeText(MainActivity.this, "Не вдалося завантажити файл", Toast.LENGTH_LONG).show();
            }
          }
        });

        if (state != null && web.restoreState(state) != null) {
            pageShown = true;  // відновили відкриту сторінку — екран підключення не потрібен
            web.removeCallbacks(loadTimeout);
            loading.setVisibility(View.GONE);
        } else verifyThenLoad(server);
    }

    /**
     * Спершу переконуємось, що за адресою саме наш сервер (server_id), і лише тоді відкриваємо сторінку з cookie входу.
     * Сервер не відповідає (роутер видав йому інший IP) — шукаємо його в мережі і переходимо на нову адресу.
     * На новій адресі WebView не має cookie входу: підкладаємо збережений токен, але лише після доказу (proof),
     * що це той самий сервер, — і людина входить без пароля.
     */
    private void verifyThenLoad(final String server) {
        final WebView target = web;
        final boolean maySearch = android.os.SystemClock.elapsedRealtime() - searchedAt > 60_000L;  // без циклу пошуків
        new Thread(new Runnable() {
            public void run() {
                final int check = WatchService.checkServer(MainActivity.this, null, server);
                String moved = null;
                if (check != WatchService.SERVER_OK && maySearch) {
                    searchedAt = android.os.SystemClock.elapsedRealtime();
                    runOnUiThread(new Runnable() {
                        public void run() { if (web == target) web.removeCallbacks(loadTimeout); }  // пошук — ще кілька секунд
                    });
                    moved = WatchService.discover(MainActivity.this, null, server);
                    if (moved != null) WatchService.moveServer(MainActivity.this, moved);
                }
                final String found = moved;
                final String token = prefs().getString("remember", null);
                final boolean inject = check == WatchService.SERVER_OK && token != null
                        && WatchService.rememberFrom(CookieManager.getInstance().getCookie(server)) == null
                        && WatchService.proven(MainActivity.this, null, server, 5_000);
                runOnUiThread(new Runnable() {
                    public void run() {
                        if (web != target || isFinishing()) return;  // за цей час відкрили інший екран
                        if (found != null) {
                            Toast.makeText(MainActivity.this, "Сервер змінив адресу — знайшов його в мережі", Toast.LENGTH_SHORT).show();
                            showWeb(found, null);
                            return;
                        }
                        web.removeCallbacks(loadTimeout);
                        web.postDelayed(loadTimeout, 15000);
                        if (inject) {
                            CookieManager.getInstance().setCookie(server, "crewhub_remember=" + token + "; Max-Age=31536000; Path=/; HttpOnly");
                            CookieManager.getInstance().flush();
                        }
                        if (check == WatchService.SERVER_OTHER) {
                            showSetup(server, label(server) + ": за цією адресою відповідає інший пристрій, а не сервер компанії."
                                    + "\nПеревірте, що телефон у робочому Wi-Fi. Якщо сервер перевстановлювали, "
                                    + "натисніть «Підключитися» або відскануйте QR-код ще раз.");
                            return;
                        }
                        target.loadUrl(server);  // сервер не відповів: WebView сам покаже помилку й екран підключення
                    }
                });
            }
        }).start();
    }

    /** Поки сторінка не відкрилась: лого, адреса сервера і кнопка змінити її (замість чорного екрана). */
    private View loadingView(final String server) {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(bgColor());
        box.setPadding(dp(28), dp(28), dp(28), dp(28));
        box.setClickable(true);
        TextView logo = new TextView(this);
        logo.setText("CrewHub");
        logo.setTextSize(30);
        logo.setTypeface(Typeface.DEFAULT_BOLD);
        logo.setTextColor(BRAND);
        logo.setGravity(Gravity.CENTER);
        box.addView(logo);
        ProgressBar spin = new ProgressBar(this);
        spin.setIndeterminate(true);
        LinearLayout.LayoutParams sp = new LinearLayout.LayoutParams(dp(40), dp(40));
        sp.topMargin = dp(18);
        sp.gravity = Gravity.CENTER_HORIZONTAL;
        box.addView(spin, sp);
        TextView t = new TextView(this);
        t.setText("Підключаюсь\n" + label(server));
        t.setTag("status");
        t.setTextColor(mutedColor());
        t.setTextSize(15);
        t.setGravity(Gravity.CENTER);
        t.setPadding(0, dp(14), 0, dp(18));
        box.addView(t);
        Button change = new Button(this);
        change.setText("Змінити підключення");
        change.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { showSetup(server, null); }
        });
        box.addView(change);
        return box;
    }

    /** Слабкий телефон (мало пам'яті): сайт одразу вмикає легкий режим анімацій. */
    private boolean weakPhone() {
        android.app.ActivityManager am = (android.app.ActivityManager) getSystemService(ACTIVITY_SERVICE);
        android.app.ActivityManager.MemoryInfo mi = new android.app.ActivityManager.MemoryInfo();
        am.getMemoryInfo(mi);
        return am.isLowRamDevice() || mi.totalMem < 3500L * 1024 * 1024;
    }

    /** Автозміна: сповіщення (Android 13+) і виняток з економії батареї, інакше фоновий пінг засинає. Питаємо один раз. */
    private void setupBackground() {
        if (!WatchService.enabled(this)) return;
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission("android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED
                && !prefs().getBoolean("asked_notif", false)) {
            prefs().edit().putBoolean("asked_notif", true).apply();
            requestPermissions(new String[]{"android.permission.POST_NOTIFICATIONS"}, 2);
        }
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        if (!pm.isIgnoringBatteryOptimizations(getPackageName()) && !prefs().getBoolean("asked_battery", false)) {
            prefs().edit().putBoolean("asked_battery", true).apply();
            new AlertDialog.Builder(this)
                    .setTitle("Автозміна по Wi-Fi")
                    .setMessage("Щоб зміна починалась і закривалась сама, CrewHub має перевіряти робочий Wi-Fi у фоні. "
                            + "Дозвольте роботу без обмежень батареї на наступному екрані. Витрата заряду мала: один короткий запит на хвилину лише під час зміни.")
                    .setPositiveButton("Дозволити", new android.content.DialogInterface.OnClickListener() {
                        public void onClick(android.content.DialogInterface d, int w) {
                            try {
                                startActivity(new Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                                        Uri.parse("package:" + getPackageName())));
                            } catch (Exception ignored) {
                            }
                            WatchService.start(MainActivity.this);
                        }
                    })
                    .setNegativeButton("Не зараз", null)
                    .show();
        }
        WatchService.start(this);
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        if (requestCode == SCAN) {
            String text = resultCode == RESULT_OK && data != null ? data.getStringExtra("text") : null;
            if (text == null) return;
            String code = decodeCode(text);
            if (text.startsWith("crewhub://") && confirmLink(Uri.parse(text), false)) return;
            if (code != null) {
                prefs().edit().putString(KEY_SERVER, code).putBoolean("hidden", true).remove("company").remove("cookie")
                        .remove("server_id").apply();
                showWeb(code, null);
                return;
            }
            Toast.makeText(this, "Це не QR-код CrewHub", Toast.LENGTH_LONG).show();
            return;
        }
        if (requestCode == ADD_WIFI) {
            new android.os.Handler().postDelayed(new Runnable() {
                public void run() { finishWifi(); }
            }, 4000);  // даємо телефону підключитися до щойно збереженої мережі
            return;
        }
        if (requestCode == PICK_FILE && fileCallback != null) {
            fileCallback.onReceiveValue(WebChromeClient.FileChooserParams.parseResult(resultCode, data));
            fileCallback = null;
            return;
        }
        super.onActivityResult(requestCode, resultCode, data);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (web != null) {
            web.resumeTimers();
            web.onResume();
            // Повернулися в застосунок: сторінка зміни одразу перевіряє мережу (visibilitychange)
        }
    }

    @Override
    protected void onPause() {
        // Застосунок згорнули: зупиняємо таймери сторінки (лічильник щосекунди, пінг кожні 20 с, live-панель).
        // У фоні присутність підтверджує лише WatchService — без подвійних пінгів і зайвої роботи процесора
        if (web != null) {
            web.onPause();
            web.pauseTimers();
        }
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        if (web != null) {
            web.removeCallbacks(loadTimeout);
            web.destroy();
            web = null;
        }
        super.onDestroy();
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        if (web != null) web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}

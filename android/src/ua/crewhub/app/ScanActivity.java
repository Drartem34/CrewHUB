package ua.crewhub.app;

import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.hardware.Camera;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.view.Gravity;
import android.view.Surface;
import android.view.SurfaceHolder;
import android.view.SurfaceView;
import android.view.View;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.google.zxing.BarcodeFormat;
import com.google.zxing.BinaryBitmap;
import com.google.zxing.DecodeHintType;
import com.google.zxing.PlanarYUVLuminanceSource;
import com.google.zxing.Result;
import com.google.zxing.common.HybridBinarizer;
import com.google.zxing.qrcode.QRCodeReader;

import java.util.Collections;
import java.util.EnumMap;
import java.util.List;
import java.util.Map;

/**
 * Сканер QR-коду підключення: камера → ZXing → текст коду повертається в MainActivity.
 * Старий Camera API навмисно: він простий і однаково працює на телефонах з Android 6 і новіших.
 */
@SuppressWarnings("deprecation")
public class ScanActivity extends Activity implements SurfaceHolder.Callback, Camera.PreviewCallback {
    private static final int CAMERA_PERMISSION = 3;

    private Camera camera;
    private SurfaceHolder holder;
    private int width, height;
    private volatile boolean decoding, done;
    private HandlerThread worker;
    private Handler bg;
    private final QRCodeReader reader = new QRCodeReader();
    private final Map<DecodeHintType, Object> hints = new EnumMap<>(DecodeHintType.class);
    private final Runnable refocus = new Runnable() {
        public void run() {
            try {
                if (camera != null) camera.autoFocus(null);
            } catch (Exception ignored) {
            }
            if (bg != null) bg.postDelayed(this, 2000);
        }
    };

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        hints.put(DecodeHintType.POSSIBLE_FORMATS, Collections.singletonList(BarcodeFormat.QR_CODE));
        hints.put(DecodeHintType.TRY_HARDER, Boolean.TRUE);
        FrameLayout frame = new FrameLayout(this);
        frame.setBackgroundColor(Color.BLACK);
        SurfaceView view = new SurfaceView(this);
        frame.addView(view, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT));
        LinearLayout bottom = new LinearLayout(this);
        bottom.setOrientation(LinearLayout.VERTICAL);
        bottom.setGravity(Gravity.CENTER_HORIZONTAL);
        bottom.setBackgroundColor(Color.parseColor("#B3000000"));
        int pad = Math.round(16 * getResources().getDisplayMetrics().density);
        bottom.setPadding(pad, pad, pad, pad);
        TextView hint = new TextView(this);
        hint.setText("Наведіть камеру на QR-код підключення.\nЙого показує адміністратор у «Точки і Wi-Fi».");
        hint.setTextColor(Color.WHITE);
        hint.setTextSize(16);
        hint.setGravity(Gravity.CENTER);
        bottom.addView(hint);
        Button cancel = new Button(this);
        cancel.setText("Скасувати");
        cancel.setOnClickListener(new View.OnClickListener() {
            public void onClick(View v) { finish(); }
        });
        bottom.addView(cancel);
        frame.addView(bottom, new FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT,
                FrameLayout.LayoutParams.WRAP_CONTENT, Gravity.BOTTOM));
        setContentView(frame);
        view.getHolder().addCallback(this);
        if (checkSelfPermission(android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED)
            requestPermissions(new String[]{android.Manifest.permission.CAMERA}, CAMERA_PERMISSION);
    }

    @Override
    public void onRequestPermissionsResult(int code, String[] perms, int[] results) {
        if (code != CAMERA_PERMISSION) return;
        if (results.length > 0 && results[0] == PackageManager.PERMISSION_GRANTED) openCamera();
        else {
            Toast.makeText(this, "Без доступу до камери введіть код підключення вручну", Toast.LENGTH_LONG).show();
            finish();
        }
    }

    @Override
    protected void onResume() {
        super.onResume();
        worker = new HandlerThread("qr");
        worker.start();
        bg = new Handler(worker.getLooper());
        openCamera();
    }

    @Override
    protected void onPause() {
        closeCamera();
        if (worker != null) worker.quitSafely();
        worker = null;
        bg = null;
        super.onPause();
    }

    public void surfaceCreated(SurfaceHolder h) {
        holder = h;
        openCamera();
    }

    public void surfaceChanged(SurfaceHolder h, int format, int w, int hh) {
    }

    public void surfaceDestroyed(SurfaceHolder h) {
        holder = null;
        closeCamera();
    }

    private void openCamera() {
        if (camera != null || holder == null || bg == null
                || checkSelfPermission(android.Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) return;
        try {
            int id = 0;
            Camera.CameraInfo info = new Camera.CameraInfo();
            for (int i = 0; i < Camera.getNumberOfCameras(); i++) {
                Camera.getCameraInfo(i, info);
                if (info.facing == Camera.CameraInfo.CAMERA_FACING_BACK) {
                    id = i;
                    break;
                }
            }
            Camera.getCameraInfo(id, info);
            camera = Camera.open(id);
            Camera.Parameters p = camera.getParameters();
            Camera.Size best = null;
            for (Camera.Size s : p.getSupportedPreviewSizes()) {  // ~720p: досить для QR і не гальмує
                if (best == null || Math.abs(s.width * s.height - 1280 * 720) < Math.abs(best.width * best.height - 1280 * 720)) best = s;
            }
            if (best != null) p.setPreviewSize(best.width, best.height);
            List<String> modes = p.getSupportedFocusModes();
            boolean continuous = false;
            if (modes.contains(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE)) {
                p.setFocusMode(Camera.Parameters.FOCUS_MODE_CONTINUOUS_PICTURE);
                continuous = true;
            } else if (modes.contains(Camera.Parameters.FOCUS_MODE_AUTO)) {
                p.setFocusMode(Camera.Parameters.FOCUS_MODE_AUTO);
            }
            camera.setParameters(p);
            Camera.Size size = camera.getParameters().getPreviewSize();
            width = size.width;
            height = size.height;
            int degrees = 0;
            switch (getWindowManager().getDefaultDisplay().getRotation()) {
                case Surface.ROTATION_90: degrees = 90; break;
                case Surface.ROTATION_180: degrees = 180; break;
                case Surface.ROTATION_270: degrees = 270; break;
                default: break;
            }
            camera.setDisplayOrientation((info.orientation - degrees + 360) % 360);
            camera.setPreviewDisplay(holder);
            camera.setPreviewCallback(this);
            camera.startPreview();
            if (!continuous) bg.postDelayed(refocus, 500);
        } catch (Exception e) {
            closeCamera();
            Toast.makeText(this, "Камера недоступна. Введіть код підключення вручну", Toast.LENGTH_LONG).show();
            finish();
        }
    }

    private void closeCamera() {
        if (bg != null) bg.removeCallbacks(refocus);
        if (camera == null) return;
        try {
            camera.setPreviewCallback(null);
            camera.stopPreview();
        } catch (Exception ignored) {
        }
        camera.release();
        camera = null;
    }

    @Override
    public void onPreviewFrame(final byte[] data, Camera cam) {
        if (decoding || done || bg == null) return;
        decoding = true;
        final int w = width, h = height;
        bg.post(new Runnable() {
            public void run() {
                try {
                    // QR розпізнається під будь-яким кутом, тож кадр не повертаємо
                    PlanarYUVLuminanceSource src = new PlanarYUVLuminanceSource(data, w, h, 0, 0, w, h, false);
                    final Result r = reader.decode(new BinaryBitmap(new HybridBinarizer(src)), hints);
                    runOnUiThread(new Runnable() {
                        public void run() { deliver(r.getText()); }
                    });
                } catch (Exception ignored) {
                    // у кадрі немає QR — чекаємо наступний
                } finally {
                    reader.reset();
                    decoding = false;
                }
            }
        });
    }

    private void deliver(String text) {
        if (done) return;
        done = true;
        setResult(RESULT_OK, new Intent().putExtra("text", text));
        finish();
    }
}

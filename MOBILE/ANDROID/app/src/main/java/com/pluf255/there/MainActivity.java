package com.pluf255.there;

import android.app.Activity;
import android.content.Intent;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.widget.Button;
import android.widget.TextView;
import android.widget.Toast;

import java.net.HttpURLConnection;
import java.net.URL;

public class MainActivity extends Activity {
    private final Handler main = new Handler(Looper.getMainLooper());
    private TextView status;
    private TextView address;
    private Button startButton;
    private Button openButton;
    private Button stopButton;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        status = findViewById(R.id.status);
        address = findViewById(R.id.address);
        startButton = findViewById(R.id.startButton);
        openButton = findViewById(R.id.openButton);
        stopButton = findViewById(R.id.stopButton);

        startButton.setOnClickListener(v -> startServer(false));
        openButton.setOnClickListener(v -> {
            if (ThereServerService.isRunning()) {
                openInBrowser(ThereServerService.getCurrentUrl());
            } else {
                startServer(true);
            }
        });
        stopButton.setOnClickListener(v -> stopServer());

        refreshState();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refreshState();
    }

    private void startServer(boolean openAfterStart) {
        status.setText("Запускаю локальный сайт...");
        Intent intent = new Intent(this, ThereServerService.class);
        intent.setAction(ThereServerService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(intent);
        } else {
            startService(intent);
        }
        waitForReady(openAfterStart, 0);
    }

    private void stopServer() {
        Intent intent = new Intent(this, ThereServerService.class);
        intent.setAction(ThereServerService.ACTION_STOP);
        startService(intent);
        main.postDelayed(this::refreshState, 250);
    }

    private void waitForReady(boolean openAfterStart, int attempt) {
        if (ThereServerService.isRunning()) {
            String url = ThereServerService.getCurrentUrl();
            if (!url.isEmpty() && healthOk(url)) {
                refreshState();
                if (openAfterStart) {
                    openInBrowser(url);
                }
                return;
            }
        }

        String error = ThereServerService.getLastError();
        if (!error.isEmpty()) {
            refreshState();
            Toast.makeText(this, error, Toast.LENGTH_LONG).show();
            return;
        }

        if (attempt >= 120) {
            status.setText("Сервер не успел запуститься");
            return;
        }
        main.postDelayed(() -> waitForReady(openAfterStart, attempt + 1), 100);
    }

    private boolean healthOk(String baseUrl) {
        try {
            HttpURLConnection connection = (HttpURLConnection) new URL(baseUrl + "healthz").openConnection();
            connection.setConnectTimeout(350);
            connection.setReadTimeout(350);
            connection.setUseCaches(false);
            int code = connection.getResponseCode();
            connection.disconnect();
            return code == 200;
        } catch (Exception ignored) {
            return false;
        }
    }

    private void openInBrowser(String url) {
        if (url == null || url.isEmpty()) {
            Toast.makeText(this, "Сайт еще не запущен", Toast.LENGTH_SHORT).show();
            return;
        }
        try {
            Intent intent = new Intent(Intent.ACTION_VIEW, Uri.parse(url));
            intent.addCategory(Intent.CATEGORY_BROWSABLE);
            startActivity(intent);
        } catch (Throwable error) {
            Toast.makeText(this, "Не удалось открыть системный браузер", Toast.LENGTH_LONG).show();
        }
    }

    private void refreshState() {
        boolean running = ThereServerService.isRunning();
        String url = ThereServerService.getCurrentUrl();
        String error = ThereServerService.getLastError();

        if (running) {
            status.setText("Сайт работает на этом телефоне");
            address.setText(url);
        } else if (!error.isEmpty()) {
            status.setText("Ошибка запуска");
            address.setText(error);
        } else {
            status.setText("Сайт выключен");
            address.setText("Нажмите \"Включить сайт\" или \"Открыть сайт\"");
        }

        startButton.setEnabled(!running);
        stopButton.setEnabled(running);
        openButton.setEnabled(true);
    }
}

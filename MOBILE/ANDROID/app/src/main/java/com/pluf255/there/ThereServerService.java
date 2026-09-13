package com.pluf255.there;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;

import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class ThereServerService extends Service {
    public static final String ACTION_START = "com.pluf255.there.START";
    public static final String ACTION_STOP = "com.pluf255.there.STOP";
    private static final String CHANNEL_ID = "there_local_server";
    private static final int NOTIFICATION_ID = 255;

    private static volatile boolean running = false;
    private static volatile String currentUrl = "";
    private static volatile String lastError = "";

    private final ExecutorService executor = Executors.newSingleThreadExecutor();

    public static boolean isRunning() {
        return running;
    }

    public static String getCurrentUrl() {
        return currentUrl == null ? "" : currentUrl;
    }

    public static String getLastError() {
        return lastError == null ? "" : lastError;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        createNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopPythonServer();
            return START_NOT_STICKY;
        }

        startForeground(NOTIFICATION_ID, buildNotification());
        if (!running) {
            startPythonServer();
        }
        return START_STICKY;
    }

    private void startPythonServer() {
        lastError = "";
        executor.execute(() -> {
            try {
                Python python = Python.getInstance();
                PyObject module = python.getModule("mobile_server");
                String dataDir = getFilesDir().getAbsolutePath() + "/there_data";
                currentUrl = module.callAttr("start_server", dataDir).toString();
                running = true;
                NotificationManager manager = getSystemService(NotificationManager.class);
                if (manager != null) {
                    manager.notify(NOTIFICATION_ID, buildNotification());
                }
            } catch (Throwable error) {
                running = false;
                currentUrl = "";
                lastError = safeMessage(error);
                stopForeground(true);
                stopSelf();
            }
        });
    }

    private void stopPythonServer() {
        executor.execute(() -> {
            try {
                if (Python.isStarted()) {
                    Python.getInstance().getModule("mobile_server").callAttr("stop_server");
                }
            } catch (Throwable ignored) {
            }
            running = false;
            currentUrl = "";
            lastError = "";
            stopForeground(true);
            stopSelf();
        });
    }

    private void createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID,
                    "THERE local server",
                    NotificationManager.IMPORTANCE_LOW
            );
            channel.setDescription("Keeps the local THERE server active while the browser is open.");
            NotificationManager manager = getSystemService(NotificationManager.class);
            if (manager != null) {
                manager.createNotificationChannel(channel);
            }
        }
    }

    private Notification buildNotification() {
        Intent openApp = new Intent(this, MainActivity.class);
        PendingIntent pendingIntent = PendingIntent.getActivity(
                this,
                0,
                openApp,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );

        Notification.Builder builder = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL_ID)
                : new Notification.Builder(this);

        return builder
                .setContentTitle("THERE")
                .setContentText(running ? "Локальный сайт работает" : "Запускаю локальный сайт")
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentIntent(pendingIntent)
                .setOngoing(true)
                .build();
    }

    private String safeMessage(Throwable error) {
        String message = error.getMessage();
        if (message == null || message.trim().isEmpty()) {
            message = error.getClass().getSimpleName();
        }
        if (message.length() > 220) {
            message = message.substring(0, 220);
        }
        return message;
    }

    @Override
    public void onDestroy() {
        if (running) {
            try {
                if (Python.isStarted()) {
                    Python.getInstance().getModule("mobile_server").callAttr("stop_server");
                }
            } catch (Throwable ignored) {
            }
        }
        running = false;
        currentUrl = "";
        executor.shutdownNow();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }
}

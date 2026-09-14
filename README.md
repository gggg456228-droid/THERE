# THERE

## Скачать

Windows EXE:
https://raw.githubusercontent.com/gggg456228-droid/THERE/main/WINDOWS_X64/THERE.exe

Windows ZIP:
https://raw.githubusercontent.com/gggg456228-droid/THERE/main/dist/THERE_WINDOWS_X64.zip

Android ARM64 APK:
https://raw.githubusercontent.com/gggg456228-droid/THERE/main/dist/THERE_ANDROID_ARM64.apk

Android ARM64 APK 2.6.10:
https://raw.githubusercontent.com/gggg456228-droid/THERE/main/dist/THERE_ANDROID_ARM64_2.6.10.apk

Текущая версия: `2.6.10`.

## Синхронизация персонажа

В интерфейсе THERE есть отдельная кнопка `Синхронизация персонажа`.

Открой лист персонажа, вставь ссылку на GitHub репозиторий или JSON и нажми `Открыть`. THERE показывает найденных в источнике персонажей. Кнопка `+` рядом с ником загружает данные выбранного персонажа в текущий открытый лист.

Успешно открытые ссылки сохраняются отдельно для каждого пользователя в данных THERE и доступны в списке `Сохранённые ссылки` после перезапуска приложения.

## Windows

Windows версия является самостоятельным `THERE.exe`. Python отдельно устанавливать не требуется. Данные пользователя хранятся отдельно в `%LOCALAPPDATA%\THERE\data` и не перезаписываются при обновлении приложения.

## Автообновление

На старте THERE читает `update/latest.json`. Версия 2.6.10 исправляет цикл обновления, который мог возникнуть, если рядом с новым EXE оставался старый `VERSION.txt`. После успешной замены EXE обновлятор также записывает новую версию рядом с программой.

## Android

Android сборка предназначена для ARM64 и использует тот же Flask код THERE, поэтому синхронизация персонажей доступна и там. Публичная APK сборка подписана отладочным ключом CI.

## iOS

Исходник iOS находится в `MOBILE/IOS`. Он открывает серверную версию THERE во внешнем браузере. Для установки нативного приложения на обычный iPhone требуется Apple подпись.

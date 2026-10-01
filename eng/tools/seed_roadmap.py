#!/usr/bin/env python3
"""
One-time seed of the initial Roadmap content (see /roadmap) - the same
material already discussed and agreed on, just filed into the shared
board instead of a separate document. Safe to re-run: it only adds rows,
so if you want to avoid duplicates, only run this once against a fresh
roadmap_items table.

Usage (run from the same directory as docker-compose.yml, with the
container already up so ./data/wifigps.db exists):
    python3 seed_roadmap.py --data-dir ./data
"""
import argparse
import os
import sqlite3
from datetime import datetime, timezone

ITEMS = [
    ("principles", "note", "done", "Иван",
     "Прозрачная токен-модель оплаты",
     "Клиент платит только за фактически потреблённые ресурсы (мощности железа + работа команды) - без фиксированных плат и подписок. По сути - оплата за запросы."),
    ("principles", "note", "done", "Иван",
     "Данные всегда работают на всю платформу",
     "Каждое устройство улучшает систему не только для своего владельца - WiFi/BLE-данные попадают в общий пул (networks, stable_networks), точность растёт для всех клиентов сразу. Архитектурно уже так устроено."),
    ("principles", "note", "done", "Иван",
     "Прошивка устройства - всегда предельно просто, навсегда",
     "Подключил -> выбрал устройство -> нажал «Прошить» -> готово. Клиент никогда не работает с файлами прошивки напрямую. Уже реализовано (/tracker/flash) и закреплено как постоянное правило проекта."),

    ("mode1", "note", "done", "Claude",
     "Инженерный режим полностью готов",
     "Map (историческая линия как дедуплицированный граф рёбер, последние 20 треков, режим Showroom по стабильным точкам), Reports, Cleanup, Algorithm Lab, Admin, Devices, Tracker (управление прошивками), Upload logs."),

    ("mode2", "task", "todo", "Claude",
     "Показать восстановленные (WiFi/BLE) участки маршрута в кабинете клиента",
     "Сейчас трек клиента строится только по status='ok' (реальный GPS). Оценка estimated_positions для участков без GPS есть в системе, но не подмешивается в отображение клиенту."),
    ("mode2", "task", "todo", "Claude",
     "Живой индикатор устройства в кабинете клиента",
     "last_seen сейчас статичный текст без автообновления. Нужна крупная точка последней позиции + периодическое обновление (30-60с) без перезагрузки страницы."),
    ("mode2", "task", "todo", "Claude",
     "Базовые отчёты для клиента",
     "Не существует вообще. Минимально: пройденное расстояние, время online/offline, доля восстановленных (не-GPS) точек за период. Состав нужно утвердить с Иваном."),

    ("mode3", "note", "done", "Claude",
     "Два типа устройств, автоматическая выгрузка, веб-прошивка - готово",
     "DEVTR (WiFi/SD) и ALTGEO GSM (GSM, без SD, автономна). /tracker + /tracker/flash - прошивка прямо из браузера, без выбора файлов клиентом. GSM: реальном времени приём с подтверждением доставки, без нагрузки на тяжёлый конвейер на каждый пакет."),
    ("mode3", "task", "in_progress", "Claude",
     "HTTPS в прошивке DEVTR (T-Pager)",
     "Добавлена проверка сертификата Let's Encrypt (ISRG Root X1) + синхронизация времени по NTP (обязательна для валидации сертификата). Логически проверено, но не прошито на реальное устройство - см. Этап B."),
    ("mode3", "task", "todo", "Claude",
     "Живой просмотр «где сейчас машина» + базовые отчёты (GSM-режим)",
     "Данные приходят в реальном времени и сохраняются, но живого отображения позиции и самих отчётов пока нет - совпадает с пробелами режима 2."),

    ("gaps", "note", "done", "Claude",
     "Сводка пробелов",
     "1) Восстановленные участки не показаны клиенту. 2) Нет живого индикатора устройства. 3) Нет базовых отчётов ни для WiFi, ни для GSM устройств."),

    ("stage_a", "task", "todo", "Claude", "Подмешать estimated_positions в трек кабинета клиента", ""),
    ("stage_a", "task", "todo", "Claude", "Живой индикатор последней позиции устройства", ""),
    ("stage_a", "task", "todo", "Claude", "Базовый отчёт для клиента (состав - см. открытые вопросы)", ""),

    ("stage_b", "task", "in_progress", "Claude",
     "Проверка GSM-прошивки (T-Call A7670) на реальном железе",
     "Компилируется без ошибок после двух найденных багов (неверный макрос модема, нехватка места под раздел). PWRKEY-последовательность и одновременная BLE-реклама+сканирование не проверены вживую."),
    ("stage_b", "task", "in_progress", "Claude",
     "Проверка HTTPS в прошивке DEVTR на реальном железе",
     "Иван тестирует T-Pager - см. заметку про версию прошивки, переданную на сервер."),
    ("stage_b", "task", "todo", "Иван",
     "Подтвердить, что собственный HTTPS/reverse proxy пропускает синхронизацию трекера",
     "Проверить отсутствие 403/редиректов при реальной синхронизации через настроенный прокси."),

    ("stage_c", "task", "todo", "Claude",
     "Подгрузка сетей на карте по видимой области вместо полного списка",
     "/api/networks сейчас отдаёт всё сразу (~6МБ на 22 тыс. сетей) - работает при 50 устройствах, но не масштабируется дальше без изменений."),
    ("stage_c", "task", "todo", "Claude",
     "Повторный нагрузочный тест на реалистичной плотности точек",
     "Последний тест использовал точки реже, чем при настоящей записи GPS."),
    ("stage_c", "task", "todo", "Claude",
     "Учёт потребления (запросы/устройство/клиент)",
     "Техническая основа под токен-модель оплаты - сейчас нигде не считается и не привязано к клиенту."),

    ("open_questions", "suggestion", "todo", "Claude",
     "Позиционирование Bober vs ALTGEO",
     "Взаимодополняющие, не конкурирующие технологии: Bober чистит треки от РЭБ и достраивает маршрут между крайними точками (только GPS, без WiFi/BLE); ALTGEO восстанавливает позицию через WiFi/BLE. Технологии в перспективе объединятся, но сначала работают самостоятельно - отдельная, более поздняя задача."),
    ("open_questions", "suggestion", "todo", "Иван",
     "Состав базового отчёта для клиента",
     "Уложиться в простой набор (пробег, время online/offline, % восстановленных точек) или сразу нужно что-то серьёзнее (PDF, экспорт, период на выбор)?"),
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default="./data")
    args = parser.parse_args()

    db_path = os.path.join(args.data_dir, "wifigps.db")
    if not os.path.isfile(db_path):
        print(f"База не найдена по пути {db_path} - запустите из папки с docker-compose.yml, "
              f"или укажите --data-dir.")
        return

    conn = sqlite3.connect(db_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    added = 0
    for section, kind, status, author, title, body in ITEMS:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), 0) AS m FROM roadmap_items WHERE section = ?", (section,)
        ).fetchone()[0]
        conn.execute("""
            INSERT INTO roadmap_items (section, kind, title, body, status, author, position, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (section, kind, title, body, status, author, max_pos + 1, now_iso))
        added += 1
    conn.commit()
    conn.close()
    print(f"Добавлено записей: {added}. Смотрите результат на /roadmap.")


if __name__ == "__main__":
    main()

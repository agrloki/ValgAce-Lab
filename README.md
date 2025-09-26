## ValgACE для Klipper

## !!!!!!!! НЕ работает смена прутка. ACE_CHANGE_TOOL TOOL - требует доработки.!!!!!

Модуль Klipper для управления устройством Anycubic ACE (подача/ретракт филамента, парковка к хотэнду, сушка, режим бесконечной катушки). Поддерживает несколько устройств одновременно.

### Возможности
- Подключение устройства ACE по Serial (явное указание `serial` в конфиге).
- Команды G-code для подачи/ретракта, парковки, смены инструмента (слота), сушки.
- Режим Infinity Spool: автоматическая смена на следующий готовый слот.
- Мульти-девайс конфигурация с глобальными инструментами T0–T7 и маппингом к локальным слотам.
 - Per-instance команды сушки: `ACE_START_DRYING_<SECTION>`, `ACE_STOP_DRYING_<SECTION>` для явного выбора устройства.

## Требования
- Klipper и Moonraker установлены и запущены как systemd-сервисы (`klipper`, `moonraker`).
- Доступ к директориям конфигурации Klipper: `~/printer_data/config` (или путь вашей системы).
- Python окружение Klipper (`~/klippy-env/bin`). Для разработки/линтинга может понадобиться `pyserial`.

## Установка
1) Клонируйте/обновите репозиторий и запустите установку:
```bash
bash "$HOME$/ValgAce-Lab/install.sh"
```
- Скрипт:
  - Проверит наличие Klipper/Moonraker/окружения.
  - Пролинкует `extras/ace.py` в `klippy/extras/ace.py`.
  - Скопирует `ace.cfg` в `~/printer_data/config/` (если файла там нет).
  - Добавит секцию Update Manager для Moonraker (обновления из репозитория).
  - Перезапустит `moonraker` и запустит `klipper`.

2) Включите конфигурацию в Klipper, добавив в `printer.cfg`:
```ini
[include ace.cfg]
```

3) Убедитесь, что вверху `printer.cfg` присутствует:
```ini
[save_variables]
filename: ~/printer_data/config/vars.cfg
```

### Деинсталляция
```bash
bash "$HOME$/ValgAce-Lab/install.sh" -u
```
После удаления модуля удалите вручную конфигурацию из `printer.cfg` и секцию `[update_manager ValgACE]` из `moonraker.conf` при необходимости.

## Настройка `ace.cfg`
Секция устройства (минимальный пример):
```ini
[ace]
serial: /dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00   ; допускается и /dev/ttyACM0 или /dev/ttyUSB0
baud: 115200
feed_speed: 25          # мм/с, подача
retract_speed: 25       # мм/с, ретракт
retract_mode: 0         # 0 — нормальный, 1 — усиленный
toolchange_retract_length: 100
park_hit_count: 5
max_dryer_temperature: 55
disable_assist_after_toolchange: True
infinity_spool_mode: False
```

Макросы для удобства (часть уже в `ace.cfg`): `T0..T3`, `FEED_ACE`, `RETRACT_ACE`, `PARK_TO_TOOLHEAD`, `START_DRYING`, `STOP_DRYING`, `INFINITY_SPOOL` и пр.

## Поддержка нескольких устройств
Каждое устройство описывается своей секцией с префиксом `ace`, например `[ace]`, `[ace second]`. Для разделения глобальных инструментов используются параметры:
- `tool_offset`: с какого глобального инструмента начинается устройство (например, 0 для первого, 4 для второго).
- `tool_slots`: сколько локальных слотов контролирует устройство (обычно 4, локальные индексы 0–3).

Пример двух устройств (T0–T3 и T4–T7):
```ini
[ace]
serial: /dev/serial/by-id/usb-ANYCUBIC_ACE_1-if00
tool_offset: 0
tool_slots: 4

[ace second]
serial: /dev/serial/by-id/usb-ANYCUBIC_ACE_2-if00
tool_offset: 4
tool_slots: 4
```

Глобальные команды смены инструмента `T0..T7` вызывают `ACE_CHANGE_TOOL TOOL=<глобальный>`. Модуль сам проверит, к какому устройству относится глобальный индекс, и преобразует его в локальный слот:
- `local_slot = global_tool - tool_offset`
- Если `global_tool` вне диапазона `[tool_offset; tool_offset + tool_slots - 1]`, устройство игнорирует команду.

Состояние выбранного инструмента хранится отдельно для каждой секции устройства в переменной `SAVE_VARIABLE` с именем `<section_name>_current_index`, что исключает конфликты между устройствами.

Важно: команды с параметром `INDEX` (`ACE_FEED`, `ACE_RETRACT`, `ACE_PARK_TO_TOOLHEAD`, `ENABLE/DISABLE_FEED_ASSIST`, `ACE_STOP_*`, `ACE_UPDATE_*`, `ACE_FILAMENT_INFO`) принимают глобальные индексы (например, 0–7). Модуль сам выберет нужное устройство и преобразует глобальный индекс к локальному слоту.

### Маршрутизация команд
- Внутри модуля действует глобальный роутер: общие команды `ACE_*` регистрируются один раз и автоматически направляются в нужный инстанс по глобальному `INDEX`/`TOOL`.
- Если устройств несколько, использовать общие команды безопасно — они сами найдут нужную секцию `[ace …]`.
- Для сушки дополнительно доступны per-instance команды `ACE_START_DRYING_<SECTION>` / `ACE_STOP_DRYING_<SECTION>` для явного выбора устройства.

### Примеры для `[ace second]` и типовой `printer.cfg`

Типовая структура `printer.cfg` с двумя устройствами:
```ini
[save_variables]
filename: ~/printer_data/config/vars.cfg

[include ace.cfg]     ; базовая секция и макросы (T0..T7, FEED_ACE и пр.)
[include ace_second.cfg]    ; при желании можно вынести вторую секцию в отдельный файл
```

Содержимое `ace_second.cfg` (пример):
```ini
[ace second]
serial: /dev/serial/by-id/usb-ANYCUBIC_ACE_2-if00
baud: 115200
feed_speed: 25
retract_speed: 25
retract_mode: 0
toolchange_retract_length: 100
park_hit_count: 5
max_dryer_temperature: 55
disable_assist_after_toolchange: True
infinity_spool_mode: True

# Разделяем глобальные инструменты: T4..T7 управляются этим устройством
tool_offset: 4
tool_slots: 4
```

Примеры использования с двумя устройствами:
- Смена на T1 (первое устройство):
```gcode
T1
```
- Смена на T6 (обслужит устройство с `tool_offset: 4`):
```gcode
T6
```
- Подача 100 мм в глобальный инструмент 5 (будет сопоставлено к локальному слоту 1 устройства с `tool_offset: 4`):
```gcode
ACE_FEED INDEX=5 LENGTH=100 SPEED=25
```
- Infinity Spool на втором устройстве:
```gcode
ACE_INFINITY_SPOOL
```

## Доступные команды G-code
- Статус/диагностика:
  - `ACE_STATUS` — выводит статус устройства.
  - `ACE_DEBUG METHOD=<method> [PARAMS=<json>]` — отправка произвольного запроса.
- Подача/ретракт:
  - `ACE_FEED INDEX=<глобальный индекс> LENGTH=<мм> [SPEED=<мм/с>]`
  - `ACE_RETRACT INDEX=<глобальный индекс> LENGTH=<мм> [SPEED=<мм/с>] [MODE=0|1]`
  - `ACE_STOP_FEED INDEX=<глобальный индекс>` / `ACE_STOP_RETRACT INDEX=<глобальный индекс>`
  - `ACE_UPDATE_FEEDING_SPEED INDEX=<глобальный индекс> SPEED=<мм/с>`
  - `ACE_UPDATE_RETRACT_SPEED INDEX=<глобальный индекс> SPEED=<мм/с>`
- Парковка к хотэнду и смена инструмента:
  - `ACE_PARK_TO_TOOLHEAD INDEX=<глобальный индекс>`
  - `ACE_CHANGE_TOOL TOOL=<глобальный индекс>` (вызывается макросами `T0..T7`)
- Режим бесконечной катушки:
  - `ACE_INFINITY_SPOOL` — переключает на следующий готовый локальный слот устройства, где команда вызвана
- Сушка:
  - `ACE_START_DRYING TEMP=<°C> DURATION=<мин>` — общий вызов (для одного устройства)
  - `ACE_STOP_DRYING` — общий вызов
  - `ACE_START_DRYING_<SECTION> TEMP=<°C> DURATION=<мин>` — для конкретной секции (например, `ACE_START_DRYING_ACE`, `ACE_START_DRYING_ACE_SECOND`)
  - `ACE_STOP_DRYING_<SECTION>` — для конкретной секции
- Информация о филаменте:
  - `ACE_FILAMENT_INFO INDEX=<глобальный индекс>`

### Макросы T0–T7
В `ace.cfg` уже добавлены:
```ini
[gcode_macro T0]
gcode:
    ACE_CHANGE_TOOL TOOL=0

[gcode_macro T1]
gcode:
    ACE_CHANGE_TOOL TOOL=1

; ... T2, T3 ...

[gcode_macro T4]
gcode:
    ACE_CHANGE_TOOL TOOL=4

[gcode_macro T5]
gcode:
    ACE_CHANGE_TOOL TOOL=5

[gcode_macro T6]
gcode:
    ACE_CHANGE_TOOL TOOL=6

[gcode_macro T7]
gcode:
    ACE_CHANGE_TOOL TOOL=7
```

## Примеры
- Подача 120 мм в глобальный инструмент 2:
```gcode
ACE_FEED INDEX=2 LENGTH=120 SPEED=25
```
- Ретракт 80 мм из глобального инструмента 6 (будет сопоставлено к локальному слоту 2 второго устройства):
```gcode
ACE_RETRACT INDEX=6 LENGTH=80 SPEED=25 MODE=1
```
- Смена на глобальный инструмент T5 (обслужит секция с `tool_offset: 4`):
```gcode
T5
```
- Запуск сушки на 55°C на 2 часа:
```gcode
ACE_START_DRYING TEMP=55 DURATION=120
```
- Infinity Spool на активном устройстве:
```gcode
ACE_INFINITY_SPOOL
```
- Сушка на устройстве из секции `[ace second]`:
```gcode
ACE_START_DRYING_ACE_SECOND TEMP=55 DURATION=120
```

## Обновления через Moonraker
Скрипт установки добавляет в `moonraker.conf` секцию Update Manager:
```ini
[update_manager ValgACE]
type: git_repo
path: /path/to/ValgAce-Lab
primary_branch: main
origin: https://github.com/agrloki/ValgAce-Lab.git
managed_services: klipper
```
После этого обновление доступно в веб-интерфейсе Moonraker (Mainsail/Fluidd) и перезапускает Klipper.

## Типичные проблемы и решения
- Устройство не находится:
  - Укажите точный путь `serial` в секции `[ace ...]` — поддерживаются как `/dev/serial/by-id/...`, так и прямые пути `/dev/ttyACM*`, `/dev/ttyUSB*`.
- Команда `Tn` не переключает инструмент:
  - Убедитесь, что `n` попадает в диапазон `[tool_offset; tool_offset + tool_slots - 1]` нужной секции.
  - Проверьте, что локальный слот готов (статус `ready`).
- Коды ошибок ACE:
  - Команды отвечают `ACE Error: <msg>` при ошибках устройства.
- Линтер жалуется на `serial`:
  - Это ожидаемо вне окружения Klipper. Для разработки установите `pyserial` в своё окружение.

## Безопасность
- `max_dryer_temperature` ограничивает максимальную температуру сушки.
- Параметры скорости и длины подачи/ретракта выбирайте исходя из механики вашего тракта.
- При Infinity Spool печать может быть приостановлена при отсутствии готовых слотов.

## Лицензия
BSD 2-Clause. См. файл `LICENSE`.



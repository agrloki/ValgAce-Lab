# ValgAce module for Klipper
# Global registry for multi-instance routing
INSTANCE_REGISTRY = []
GLOBAL_COMMANDS_REGISTERED = False
import os
import serial
import json
import struct
import queue
import traceback
from typing import Optional, Dict, Any, Callable
from serial import SerialException
class ValgAce:
    PARK_TIMEOUT = 30.0      # seconds
    REQUEST_TIMEOUT = 5.0    # seconds
    SLOT_READY_TIMEOUT = 10.0  # max wait time for slot to become ready
    def __init__(self, config):
        self.printer = config.get_printer()
        self.toolhead = None
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object('gcode')
        self._name = config.get_name()
        self.variables = self.printer.lookup_object('save_variables').allVariables
        self.read_buffer = bytearray()
        self.send_time = 0
        self._last_status_request = 0
        # Параметры таймаутов
        self._response_timeout = config.getfloat('response_timeout', 2.0)
        self._read_timeout = config.getfloat('read_timeout', 0.1)
        self._write_timeout = config.getfloat('write_timeout', 0.5)
        self._max_queue_size = config.getint('max_queue_size', 20)
        # Serial обязателен
        self.serial_name = config.get('serial')
        if not self.serial_name:
            raise self.printer.config_error("ValgAce: 'serial' must be specified in the [ace] section")
        self.baud = config.getint('baud', 115200)
        # Конфигурация параметров
        self.feed_speed = config.getint('feed_speed', 50)
        self.retract_speed = config.getint('retract_speed', 50)
        self.retract_mode = config.getint('retract_mode', 0)
        self.toolchange_retract_length = config.getint('toolchange_retract_length', 100)
        self.park_hit_count = config.getint('park_hit_count', 5)
        self.max_dryer_temperature = config.getint('max_dryer_temperature', 55)
        self.disable_assist_after_toolchange = config.getboolean('disable_assist_after_toolchange', True)
        self.infinity_spool_mode = config.getboolean('infinity_spool_mode', False)
        # Глобальное маппинг инструментов
        self.tool_offset = config.getint('tool_offset', 0)
        self.tool_slots = config.getint('tool_slots', 4)
        # Состояние устройства
        self._info = self._get_default_info()
        self._callback_map = {}
        self._request_id = 0
        self._connected = False
        self._connection_attempts = 0
        self._max_connection_attempts = 5
        # Работа
        self._feed_assist_index = -1
        self._last_assist_count = 0
        self._assist_hit_count = 0
        self._park_in_progress = False
        self._park_is_toolchange = False
        self._park_previous_tool = -1
        self._park_index = -1
        self._park_start_time = 0.0
        # Таймеры
        self._park_timeout_timer = None
        self._request_timeout_timers = {}
        # Очереди задач
        self._queue = queue.Queue(maxsize=self._max_queue_size)
        self._main_queue = queue.Queue()
        # Serial и реактор
        self._serial = None
        self._reader_timer = None
        self._writer_timer = None
        # Регистрация событий
        self._register_handlers()
        self._register_gcode_commands()
        # Запуск таймеров подключения и обработки очереди
        self.reactor.register_timer(self._connect_check, self.reactor.NOW)
        self.reactor.register_timer(self._main_eval, self.reactor.NOW)
        # Регистрация экземпляра
        global INSTANCE_REGISTRY
        INSTANCE_REGISTRY.append(self)
    def _make_cmd_suffix(self, section_name: str) -> str:
        safe = ''.join(ch if ch.isalnum() else '_' for ch in section_name).upper()
        return safe
    def _get_default_info(self) -> Dict[str, Any]:
        return {
            'status': 'disconnected',
            'dryer': {
                'status': 'stop',
                'target_temp': 0,
                'duration': 0,
                'remain_time': 0
            },
            'temp': 0,
            'enable_rfid': 1,
            'fan_speed': 7000,
            'feed_assist_count': 0,
            'cont_assist_time': 0.0,
            'slots': [{
                'index': i,
                'status': 'empty',
                'sku': '',
                'type': '',
                'color': [0, 0, 0]
            } for i in range(4)]
        }
    def _register_handlers(self):
        self.printer.register_event_handler('klippy:ready', self._handle_ready)
        self.printer.register_event_handler('klippy:disconnect', self._handle_disconnect)
    def _register_gcode_commands(self):
        global GLOBAL_COMMANDS_REGISTERED
        if not GLOBAL_COMMANDS_REGISTERED:
            commands = [
                ('ACE_DEBUG', self.cmd_ACE_DEBUG, "Debug connection"),
                ('ACE_STATUS', self.cmd_ACE_STATUS, "Get device status"),
                ('ACE_START_DRYING', self.cmd_ACE_START_DRYING, "Start drying"),
                ('ACE_STOP_DRYING', self.cmd_ACE_STOP_DRYING, "Stop drying"),
                ('ACE_ENABLE_FEED_ASSIST', self.router_ENABLE_FEED_ASSIST, "Enable feed assist (routed)"),
                ('ACE_DISABLE_FEED_ASSIST', self.router_DISABLE_FEED_ASSIST, "Disable feed assist (routed)"),
                ('ACE_PARK_TO_TOOLHEAD', self.router_PARK_TO_TOOLHEAD, "Park filament to toolhead (routed)"),
                ('ACE_FEED', self.router_FEED, "Feed filament (routed)"),
                ('ACE_UPDATE_FEEDING_SPEED', self.router_UPDATE_FEEDING_SPEED, "Update feeding speed (routed)"),
                ('ACE_STOP_FEED', self.router_STOP_FEED, "Stop feed filament (routed)"),
                ('ACE_RETRACT', self.router_RETRACT, "Retract filament (routed)"),
                ('ACE_UPDATE_RETRACT_SPEED', self.router_UPDATE_RETRACT_SPEED, "Update retracting speed (routed)"),
                ('ACE_STOP_RETRACT', self.router_STOP_RETRACT, "Stop retract filament (routed)"),
                ('ACE_CHANGE_TOOL', self.router_CHANGE_TOOL, "Change tool (routed)"),
                ('ACE_FILAMENT_INFO', self.router_FILAMENT_INFO, "Show filament info (routed)"),
            ]
            for name, func, desc in commands:
                self.gcode.register_command(name, func, desc=desc)
            GLOBAL_COMMANDS_REGISTERED = True
        suffix = self._make_cmd_suffix(self._name)
        if suffix:
            self.gcode.register_command(
                f'ACE_START_DRYING_{suffix}', self.cmd_ACE_START_DRYING, desc=f"Start drying ({self._name})"
            )
            self.gcode.register_command(
                f'ACE_STOP_DRYING_{suffix}', self.cmd_ACE_STOP_DRYING, desc=f"Stop drying ({self._name})"
            )
    # ---- Маршрутизация по индексам ----
    def _find_instance_by_global_index(self, g_index: int):
        for inst in INSTANCE_REGISTRY:
            if g_index >= inst.tool_offset and g_index < inst.tool_offset + inst.tool_slots:
                return inst
        return None
    def _instance_and_local_index(self, g_index: int):
        inst = self._find_instance_by_global_index(g_index)
        if inst is None:
            return None, None
        return inst, g_index - inst.tool_offset
    # --- Реализация недостающих команд ---
    def cmd_ACE_FEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            gcmd.respond_raw(f"Index {g_index} out of range for this instance")
            return
        index = g_index - self.tool_offset
        length = gcmd.get_int('LENGTH', self.feed_speed) # Предполагаем, что длина по умолчанию равна скорости
        speed = gcmd.get_int('SPEED', self.feed_speed)

        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Feed Error: {response.get('msg', 'Unknown error')}")
                else:
                    gcmd.respond_info(f"Feed command sent to slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Feed callback error: {str(e)}")

        self.send_request({"method": "feed", "params": {"index": index, "length": length, "speed": speed}}, callback)

    def cmd_ACE_UPDATE_FEEDING_SPEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            gcmd.respond_raw(f"Index {g_index} out of range for this instance")
            return
        index = g_index - self.tool_offset
        new_speed = gcmd.get_int('SPEED', minval=0)

        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Update Feeding Speed Error: {response.get('msg', 'Unknown error')}")
                else:
                    self.feed_speed = new_speed # Обновляем локальный параметр
                    gcmd.respond_info(f"Feeding speed updated to {new_speed} for slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Update Feeding Speed callback error: {str(e)}")

        self.send_request({"method": "update_feeding_speed", "params": {"index": index, "speed": new_speed}}, callback)

    def cmd_ACE_STOP_FEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            gcmd.respond_raw(f"Index {g_index} out of range for this instance")
            return
        index = g_index - self.tool_offset

        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Stop Feed Error: {response.get('msg', 'Unknown error')}")
                else:
                    gcmd.respond_info(f"Stop feed command sent to slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Stop Feed callback error: {str(e)}")

        self.send_request({"method": "stop_feed", "params": {"index": index}}, callback)

    def cmd_ACE_RETRACT(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            gcmd.respond_raw(f"Index {g_index} out of range for this instance")
            return
        index = g_index - self.tool_offset
        length = gcmd.get_int('LENGTH', self.toolchange_retract_length) # Используем параметр из конфига
        speed = gcmd.get_int('SPEED', self.retract_speed)

        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Retract Error: {response.get('msg', 'Unknown error')}")
                else:
                    gcmd.respond_info(f"Retract command sent to slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Retract callback error: {str(e)}")

        self.send_request({"method": "retract", "params": {"index": index, "length": length, "speed": speed}}, callback)

    def cmd_ACE_UPDATE_RETRACT_SPEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            gcmd.respond_raw(f"Index {g_index} out of range for this instance")
            return
        index = g_index - self.tool_offset
        new_speed = gcmd.get_int('SPEED', minval=0)

        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Update Retract Speed Error: {response.get('msg', 'Unknown error')}")
                else:
                    self.retract_speed = new_speed # Обновляем локальный параметр
                    gcmd.respond_info(f"Retract speed updated to {new_speed} for slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Update Retract Speed callback error: {str(e)}")

        self.send_request({"method": "update_retract_speed", "params": {"index": index, "speed": new_speed}}, callback)

    def cmd_ACE_STOP_RETRACT(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            gcmd.respond_raw(f"Index {g_index} out of range for this instance")
            return
        index = g_index - self.tool_offset

        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Stop Retract Error: {response.get('msg', 'Unknown error')}")
                else:
                    gcmd.respond_info(f"Stop retract command sent to slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Stop Retract callback error: {str(e)}")

        self.send_request({"method": "stop_retract", "params": {"index": index}}, callback)

    # --- Конец реализации команд ---
    def router_FEED(self, gcmd): ...
    def router_UPDATE_FEEDING_SPEED(self, gcmd): ...
    def router_STOP_FEED(self, gcmd): ...
    def router_RETRACT(self, gcmd): ...
    def router_UPDATE_RETRACT_SPEED(self, gcmd): ...
    def router_STOP_RETRACT(self, gcmd): ...
    def router_PARK_TO_TOOLHEAD(self, gcmd): ...
    def router_ENABLE_FEED_ASSIST(self, gcmd): ...
    def router_DISABLE_FEED_ASSIST(self, gcmd): ...
    def router_FILAMENT_INFO(self, gcmd): ...
    def router_CHANGE_TOOL(self, gcmd): ...
    # Реализация роутеров
    def router_FEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_FEED(gcmd)
    def router_UPDATE_FEEDING_SPEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_UPDATE_FEEDING_SPEED(gcmd)
    def router_STOP_FEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_STOP_FEED(gcmd)
    def router_RETRACT(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_RETRACT(gcmd)
    def router_UPDATE_RETRACT_SPEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_UPDATE_RETRACT_SPEED(gcmd)
    def router_STOP_RETRACT(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_STOP_RETRACT(gcmd)
    def router_PARK_TO_TOOLHEAD(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_PARK_TO_TOOLHEAD(gcmd)
    def router_ENABLE_FEED_ASSIST(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_ENABLE_FEED_ASSIST(gcmd)
    def router_DISABLE_FEED_ASSIST(self, gcmd):
        idx_val = gcmd.get('INDEX', None)
        if idx_val is None:
            for inst in INSTANCE_REGISTRY:
                if inst._feed_assist_index >= 0:
                    inst.cmd_ACE_DISABLE_FEED_ASSIST(gcmd)
            return
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_DISABLE_FEED_ASSIST(gcmd)
    def router_FILAMENT_INFO(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        inst, _ = self._instance_and_local_index(g_index)
        if inst is None: return
        inst.cmd_ACE_FILAMENT_INFO(gcmd)
    def router_CHANGE_TOOL(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=-1, maxval=255)
        if tool == -1:
            for inst in INSTANCE_REGISTRY:
                inst.cmd_ACE_CHANGE_TOOL(gcmd)
            return
        for inst in INSTANCE_REGISTRY:
            if tool >= inst.tool_offset and tool < inst.tool_offset + inst.tool_slots:
                inst.cmd_ACE_CHANGE_TOOL(gcmd)
                return
    # === Ключевые методы с защитой от None ===
    def _connect_check(self, eventtime):
        try:
            if not self._connected:
                self._connect()
        except Exception as e:
            self.gcode.respond_info(f"[ACE] Error in _connect_check: {e}")
            traceback.print_exc()
        return eventtime + 1.0  # ВСЕГДА возвращаем float
    def _connect(self) -> bool:
        if self._connected:
            return True
        for attempt in range(self._max_connection_attempts):
            try:
                self._serial = serial.Serial(
                    port=self.serial_name,
                    baudrate=self.baud,
                    timeout=0,
                    write_timeout=self._write_timeout
                )
                if self._serial.is_open:
                    self._connected = True
                    self._info['status'] = 'ready'
                    self.gcode.respond_info(f"Connected to ACE at {self.serial_name}")
                    def info_callback(response):
                        res = response.get('result', {})
                        model = res.get('model', 'Unknown')
                        firmware = res.get('firmware', 'Unknown')
                        self.gcode.respond_info(f"Device info: {model} {firmware}")
                        self.gcode.respond_info(f"Connected {model} {firmware}")
                    self.send_request({"method": "get_info"}, info_callback)
                    if self._reader_timer is None:
                        self._reader_timer = self.reactor.register_timer(self._reader_loop, self.reactor.NOW)
                    if self._writer_timer is None:
                        self._writer_timer = self.reactor.register_timer(self._writer_loop, self.reactor.NOW)
                    return True
            except Exception as e:
                self.gcode.respond_info(f"Connection attempt {attempt + 1} failed: {str(e)}")
                traceback.print_exc()
                self.dwell(1.0, None)
        self.gcode.respond_info("Failed to connect to ACE device")
        return False
    def _disconnect(self):
        if not self._connected:
            return
        self._connected = False
        if self._reader_timer:
            self.reactor.unregister_timer(self._reader_timer)
            self._reader_timer = None
        if self._writer_timer:
            self.reactor.unregister_timer(self._writer_timer)
            self._writer_timer = None
        try:
            if self._serial and self._serial.is_open:
                self._serial.close()
        except Exception as e:
            self.gcode.respond_info(f"Disconnect error: {str(e)}")
        if self._park_timeout_timer:
            self.reactor.unregister_timer(self._park_timeout_timer)
            self._park_timeout_timer = None
        for timer in list(self._request_timeout_timers.values()):
            self.reactor.unregister_timer(timer)
        self._request_timeout_timers.clear()
    def _reconnect(self):
        self._disconnect()
        self.dwell(1.0, self._connect)
    def _reset_connection(self):
        self._disconnect()
        self.dwell(1.0, self._connect)
    def _handle_ready(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        if self.toolhead is None:
            raise self.printer.config_error("Toolhead not found in ValgAce module")
    def _handle_disconnect(self):
        self._disconnect()
    def _calc_crc(self, buffer: bytes) -> int:
        crc = 0xffff
        for byte in buffer:
            data = byte ^ (crc & 0xff)
            data ^= (data & 0x0f) << 4
            crc = ((data << 8) | (crc >> 8)) ^ (data >> 4) ^ (data << 3)
        return crc & 0xffff
    def send_request(self, request: Dict[str, Any], callback: Callable):
        if self._queue.qsize() >= self._max_queue_size:
            self.gcode.respond_info("Request queue overflow, clearing...")
            while not self._queue.empty():
                _, cb = self._queue.get_nowait()
                if cb:
                    try:
                        cb({'error': 'Queue overflow'})
                    except: pass
        request['id'] = self._get_next_request_id()
        self._queue.put((request, callback))
        req_id = request['id']
        # Используем lambda, которая вызывает _on_request_timeout и возвращает NEVER
        # Это гарантирует, что таймер завершится корректно, не оставляя None в системе таймеров.
        timer = self.reactor.register_timer(
            lambda eventtime, req_id=req_id: self._on_request_timeout_and_return_never(req_id),
            self.reactor.monotonic() + self.REQUEST_TIMEOUT
        )
        self._request_timeout_timers[req_id] = timer

    def _get_next_request_id(self) -> int:
        self._request_id += 1
        if self._request_id >= 300000:
            self._request_id = 0
        return self._request_id

    def _on_request_timeout(self, req_id):
        # Оригинальная логика таймаута, не возвращает значение (None)
        cb = self._callback_map.pop(req_id, None)
        if cb:
            try:
                cb({'error': 'Request timeout', 'id': req_id})
            except: pass
        self._request_timeout_timers.pop(req_id, None)

    # Вспомогательный метод для регистрации таймера
    def _on_request_timeout_and_return_never(self, req_id):
        # Вызывает оригинальную логику таймаута и возвращает NEVER для реактора
        self._on_request_timeout(req_id)
        return self.reactor.NEVER # <-- Явно возвращаем NEVER

    def _send_request(self, request: Dict[str, Any]) -> bool:
        try:
            payload = json.dumps(request).encode('utf-8')
        except Exception as e:
            self.gcode.respond_info(f"JSON encoding error: {str(e)}")
            return False
        crc = self._calc_crc(payload)
        packet = (
            bytes([0xFF, 0xAA]) +
            struct.pack('<H', len(payload)) +
            payload +
            struct.pack('<H', crc) +
            bytes([0xFE])
        )
        try:
            if self._serial and self._serial.is_open:
                self._serial.write(packet)
                return True
            else:
                raise SerialException("Serial port closed")
        except SerialException as e:
            self.gcode.respond_info(f"Send error: {str(e)}")
            self._reconnect()
            return False
    def _reader_loop(self, eventtime):
        try:
            if not self._connected or not self._serial or not self._serial.is_open:
                return eventtime + 0.01
            raw_bytes = self._serial.read(16)
            if raw_bytes:
                self.read_buffer.extend(raw_bytes)
                self._process_messages()
        except Exception as e:
            self.gcode.respond_info(f"Read error: {str(e)}")
            traceback.print_exc()
            self._reconnect()
        return eventtime + 0.01
    def _process_messages(self):
        incomplete_message_count = 0
        max_incomplete_messages_before_reset = 10
        while self.read_buffer:
            end_idx = self.read_buffer.find(b'\xfe')
            if end_idx == -1:
                break
            msg = self.read_buffer[:end_idx+1]
            self.read_buffer = self.read_buffer[end_idx+1:]
            if len(msg) < 7 or msg[0:2] != bytes([0xFF, 0xAA]):
                continue
            payload_len = struct.unpack('<H', msg[2:4])[0]
            expected_length = 4 + payload_len + 3
            if len(msg) < expected_length:
                self.gcode.respond_info(f"Incomplete message received (expected {expected_length}, got {len(msg)})")
                incomplete_message_count += 1
                if incomplete_message_count > max_incomplete_messages_before_reset:
                    self.gcode.respond_info("Too many incomplete messages, resetting connection")
                    self._reset_connection()
                    incomplete_message_count = 0
                continue
            incomplete_message_count = 0
            payload = msg[4:4+payload_len]
            crc = struct.unpack('<H', msg[4+payload_len:4+payload_len+2])[0]
            if crc != self._calc_crc(payload):
                continue
            try:
                response = json.loads(payload.decode('utf-8'))
                self._handle_response(response)
            except json.JSONDecodeError as je:
                self.gcode.respond_info(f"JSON decode error: {str(je)} Data: {msg}")
            except Exception as e:
                self.gcode.respond_info(f"Message processing error: {str(e)} Data: {msg}")
    def _writer_loop(self, eventtime):
        try:
            if not self._connected:
                return eventtime + 0.05
            now = eventtime
            if now - self._last_status_request > (0.2 if self._park_in_progress else 1.0):
                self._request_status()
                self._last_status_request = now
            if not self._queue.empty():
                task = self._queue.get_nowait()
                if task:
                    request, callback = task
                    self._callback_map[request['id']] = callback
                    if not self._send_request(request):
                        self.gcode.respond_info("Failed to send request, requeuing...")
                        self._queue.put(task)
        except Exception as e:
            self.gcode.respond_info(f"Writer loop error: {str(e)}")
            traceback.print_exc()
        return eventtime + 0.05
    def _request_status(self):
        def status_callback(response):
            try:
                if 'result' in response:
                    self._info.update(response['result'])
            except Exception as e:
                self.gcode.respond_info(f"Status callback error: {str(e)}")
        now = self.reactor.monotonic()
        if now - self._last_status_request > (0.2 if self._park_in_progress else 1.0):
            self.send_request({"method": "get_status"}, status_callback)
            self._last_status_request = now
            
    def _handle_response(self, response: dict):
        req_id = response.get('id')
        if req_id is not None:
            timer = self._request_timeout_timers.pop(req_id, None)
            if timer:
                self.reactor.unregister_timer(timer)
            callback = self._callback_map.pop(req_id, None)
            if callback:
                try:
                    callback(response)
                except Exception as e:
                    self.gcode.respond_raw(f"!! [ACE] _handle_response: Callback error: {str(e)}") # Debug
                    traceback.print_exc()

        if 'result' in response and isinstance(response['result'], dict):
            result = response['result']
            self._info.update(result)

            if self._park_in_progress:
                current_status = result.get('status', 'unknown')
                current_assist_count = result.get('feed_assist_count', 0)
                self.gcode.respond_info(f"// [ACE] _handle_response: Park in progress. Status: '{current_status}', Count: {current_assist_count}") # Debug

                # --- Ключевое изменение: проверка статуса ---
                if current_status == 'ready':
                    self.gcode.respond_info(f"// [ACE] _handle_response: Status is 'ready', checking count stability. Last: {self._last_assist_count}, Current: {current_assist_count}") # Debug
                    if current_assist_count != self._last_assist_count:
                        # Счётчик изменился (предположительно увеличился, если assist *только что* завершил работу и установил итоговое значение)
                        # Или, если assist *ещё работает* в 'ready' state, счётчик может меняться.
                        # Но логично предположить, что в 'ready' счётчик *стабилизировался*.
                        # Если он *изменился*, возможно, устройство ещё не до конца стабилизировалось.
                        # В ace_simple.txt он сбрасывает hit_count при изменении.
                        self.gcode.respond_info(f"// [ACE] Park: assist count changed in 'ready' state from {self._last_assist_count} to {current_assist_count}") # Debug
                        self._last_assist_count = current_assist_count
                        self._assist_hit_count = 0 # Сброс счётчика "неизменений"
                        # self.dwell(0.7, lambda: None) # <-- Опционально, как в ace_simple.txt
                    else:
                        # Счётчик не изменился по сравнению с последним сохранённым В СОСТОЯНИИ 'ready'
                        # Это означает, что assist, вероятно, завершил работу и остановился.
                        self.gcode.respond_info(f"// [ACE] Park: assist count unchanged ({current_assist_count}) in 'ready' state, hit count: {self._assist_hit_count}") # Debug
                        self._assist_hit_count += 1
                        if self._assist_hit_count >= self.park_hit_count:
                            self.gcode.respond_info(f"// [ACE] Park: hit count reached {self.park_hit_count} in 'ready' state, stopping assist.") # Debug
                            self._complete_parking(success=True)
                            return # Важно: выйти из обработки, чтобы не проверять таймаут ниже
                else:
                    self.gcode.respond_info(f"// [ACE] _handle_response: Status is '{current_status}', not 'ready'. Park logic not active for this status.") # Debug
                # Проверка таймаута парковки УБРАНА
                # now = self.reactor.monotonic()
                # if now - self._park_start_time > self.PARK_TIMEOUT:
                #     print(f"[ACE] Park: Timeout reached ({self.PARK_TIMEOUT}s).") # Debug
                #     self._complete_parking(success=False, error="Parking timeout")


    # def _start_park_timeout(self):
    #     # УБРАНО: функция больше не используется
    #     pass


    def _complete_parking(self, success=True, error=None):
        self.gcode.respond_info(f"// [ACE] _complete_parking: Called with success={success}, error={error}") # Debug
        if not self._park_in_progress:
            self.gcode.respond_raw(f"!! [ACE] _complete_parking: Warning: Called but _park_in_progress is False. Ignoring.") # Debug
            return
        # if self._park_timeout_timer: # УБРАНО: таймер больше не используется
        #     print(f"[ACE] _complete_parking: Unregistering park timeout timer.") # Debug
        #     self.reactor.unregister_timer(self._park_timeout_timer)
        #     self._park_timeout_timer = None
        self.gcode.respond_info(f"// [ACE] _complete_parking: Stopping feed assist for slot {self._park_index}.") # Debug
        try:
            if success:
                self.gcode.respond_info(f"// [ACE] _complete_parking: Sending stop_feed_assist command.") # Debug
                self.send_request({
                    "method": "stop_feed_assist",
                    "params": {"index": self._park_index}
                }, lambda r: self.gcode.respond_info(f"// [ACE] _complete_parking: stop_feed_assist command sent, response: {r}")) # Debug callback
            else:
                self.gcode.respond_info(f"// [ACE] _complete_parking: Parking failed ({error}), still sending stop_feed_assist command as a safety measure.") # Debug
                self.send_request({
                    "method": "stop_feed_assist",
                    "params": {"index": self._park_index}
                }, lambda r: self.gcode.respond_info(f"// [ACE] _complete_parking (fail): stop_feed_assist command sent, response: {r}")) # Debug callback
        except Exception as e:
            self.gcode.respond_raw(f"!! [ACE] _complete_parking: Error sending stop_feed_assist: {str(e)}") # Debug
        finally:
            self.gcode.respond_info(f"// [ACE] _complete_parking: Resetting parking flags.") # Debug
            self._park_in_progress = False
            self._park_is_toolchange = False
            self._park_previous_tool = -1
            self._park_index = -1
            if self.disable_assist_after_toolchange:
                self._feed_assist_index = -1
        self.gcode.respond_info(f"// [ACE] _complete_parking: Finished.") # Debug

                
    def dwell(self, delay: float = 1.0, callback: Optional[Callable] = None):
        """Асинхронная задержка через reactor"""
        if delay <= 0 or callback is None:
            if callback:
                self._main_queue.put(callback)
            return
        def timer_handler(eventtime):
            try:
                if callback:
                    callback()
            except Exception as e:
                self.gcode.respond_info(f"[ACE] Dwell callback error: {e}")
                traceback.print_exc()
            return self.reactor.NEVER
        wake_time = self.reactor.monotonic() + delay
        self.reactor.register_timer(timer_handler, wake_time)
    def _main_eval(self, eventtime):
        try:
            while not self._main_queue.empty():
                try:
                    task = self._main_queue.get_nowait()
                    if callable(task):
                        task()
                except Exception as e:
                    self.gcode.respond_info(f"Main eval error: {str(e)}")
                    traceback.print_exc()
        except Exception as e:
            self.gcode.respond_info(f"Critical error in _main_eval: {str(e)}")
            traceback.print_exc()
        return eventtime + 0.1
    def cmd_ACE_STATUS(self, gcmd):
        try:
            status = json.dumps(self._info, indent=2)
            gcmd.respond_info(f"ACE Status:{status}")
        except Exception as e:
            self.gcode.respond_info(f"Status command error: {str(e)}")
            gcmd.respond_raw("Error retrieving status")
    def cmd_ACE_DEBUG(self, gcmd):
        method = gcmd.get('METHOD')
        params = gcmd.get('PARAMS', '{}')
        try:
            request = {"method": method}
            if params.strip():
                request["params"] = json.loads(params)
            def callback(response):
                try:
                    if 'result' in response:
                        result = response['result']
                        output = []
                        if method == "get_info":
                            output.append("=== Device Info ===")
                            output.append(f"Model: {result.get('model', 'Unknown')}")
                            output.append(f"Firmware: {result.get('firmware', 'Unknown')}")
                            output.append(f"Hardware: {result.get('hardware', 'Unknown')}")
                            output.append(f"Serial: {result.get('serial', 'Unknown')}")
                        else:
                            output.append("=== Status ===")
                            output.append(f"State: {result.get('status', 'Unknown')}")
                            output.append(f"Temperature: {result.get('temp', 'Unknown')}")
                            output.append(f"Fan Speed: {result.get('fan_speed', 'Unknown')}")
                            for slot in result.get('slots', []):
                                output.append(f"Slot {slot.get('index', '?')}:")
                                output.append(f"  Status: {slot.get('status', 'Unknown')}")
                                output.append(f"  Type: {slot.get('type', 'Unknown')}")
                                output.append(f"  Color: {slot.get('color', 'Unknown')}")
                        gcmd.respond_info("".join(output))
                    else:
                        gcmd.respond_info(json.dumps(response, indent=2))
                except Exception as e:
                    self.gcode.respond_info(f"Debug callback error: {str(e)}")
            self.send_request(request, callback)
        except Exception as e:
            self.gcode.respond_info(f"Debug command error: {str(e)}")
            gcmd.respond_raw(f"Error: {str(e)}")
            
    def cmd_ACE_FILAMENT_INFO(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            try:
                if 'result' in response:
                    slot_info = response['result']
                    self.gcode.respond_info(str(slot_info))
                else:
                    self.gcode.respond_info('Error: No result in response')
            except Exception as e:
                self.gcode.respond_info(f"Filament info callback error: {str(e)}")
        self.send_request({"method": "get_filament_info", "params": {"index": index}}, callback)
        
    def cmd_ACE_START_DRYING(self, gcmd):
        temperature = gcmd.get_int('TEMP', minval=20, maxval=self.max_dryer_temperature)
        duration = gcmd.get_int('DURATION', 240, minval=1)
        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
                else:
                    gcmd.respond_info(f"Drying started at {temperature}°C for {duration} minutes")
            except Exception as e:
                self.gcode.respond_info(f"Start drying callback error: {str(e)}")
        self.send_request({
            "method": "drying",
            "params": {
                "temp": temperature,
                "fan_speed": 7000,
                "duration": duration * 60
            }
        }, callback)
        
    def cmd_ACE_STOP_DRYING(self, gcmd):
        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
                else:
                    gcmd.respond_info("Drying stopped")
            except Exception as e:
                self.gcode.respond_info(f"Stop drying callback error: {str(e)}")
        self.send_request({"method": "drying_stop"}, callback)
        
    def cmd_ACE_FEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        length = gcmd.get_int('LENGTH', minval=1)
        speed = gcmd.get_int('SPEED', self.feed_speed, minval=1)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "feed_filament",
            "params": {"index": index, "length": length, "speed": speed}
        }, callback)
        self.dwell((length / speed) + 0.1, lambda: None)

    def cmd_ACE_UPDATE_FEEDING_SPEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        speed = gcmd.get_int('SPEED', self.feed_speed, minval=1)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "update_feeding_speed",
            "params": {"index": index, "speed": speed}
        }, callback)
        self.dwell(0.5, lambda: None)

    def cmd_ACE_STOP_FEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info("Feed stopped")
        self.send_request({
            "method": "stop_feed_filament",
            "params": {"index": index},
        }, callback)
        self.dwell(0.5, lambda: None)
        
    def cmd_ACE_ENABLE_FEED_ASSIST(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
                else:
                    self._feed_assist_index = index
                    gcmd.respond_info(f"Feed assist enabled for slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Enable assist callback error: {str(e)}")
        self.send_request({"method": "start_feed_assist", "params": {"index": index}}, callback)
        
    def cmd_ACE_DISABLE_FEED_ASSIST(self, gcmd):
        idx_val = gcmd.get('INDEX', None)
        if idx_val is None:
            index = self._feed_assist_index
            if index < 0:
                return
        else:
            g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
            if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
                return
            index = g_index - self.tool_offset
        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
                else:
                    self._feed_assist_index = -1
                    gcmd.respond_info(f"Feed assist disabled for slot {index}")
            except Exception as e:
                self.gcode.respond_info(f"Disable assist callback error: {str(e)}")
        self.send_request({"method": "stop_feed_assist", "params": {"index": index}}, callback)
        
    def cmd_ACE_RETRACT(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        length = gcmd.get_int('LENGTH', minval=1)
        speed = gcmd.get_int('SPEED', self.retract_speed, minval=1)
        mode = gcmd.get_int('MODE', self.retract_mode, minval=0, maxval=1)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "unwind_filament",
            "params": {"index": index, "length": length, "speed": speed, "mode": mode}
        }, callback)
        self.dwell((length / speed) + 0.1, lambda: None)

    def cmd_ACE_UPDATE_RETRACT_SPEED(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        speed = gcmd.get_int('SPEED', self.feed_speed, minval=1)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
        self.send_request({
            "method": "update_unwinding_speed",
            "params": {"index": index, "speed": speed}
        }, callback)
        self.dwell(0.5, lambda: None)

    def cmd_ACE_STOP_RETRACT(self, gcmd):
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        def callback(response):
            if response.get('code', 0) != 0:
                gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            else:
                gcmd.respond_info("Retract stopped")
        self.send_request({
            "method": "stop_unwind_filament",
            "params": {"index": index},
        }, callback)
        self.dwell(0.5, lambda: None)
        
    def _park_to_toolhead(self, index: int):
        if self._park_in_progress:
            self.gcode.respond_raw("!! [ACE] _park_to_toolhead: Attempt to park while already parking. Aborting.")
            return
        self.gcode.respond_info(f"// [ACE] _park_to_toolhead: Starting park for slot {index}.")

        def start_assist_callback(response):
            self.gcode.respond_info(f"// [ACE] _park_to_toolhead: Callback for start_feed_assist received. Response: {response}") # Debug
            try:
                if response.get('code', 0) != 0:
                    err_msg = response.get('msg', 'Unknown error')
                    self._complete_parking(success=False, error=f"ACE Error: {err_msg}")
                    return
                # --- Ключевое изменение: получение начального значения из ответа start_feed_assist ---
                # Пытаемся получить feed_assist_count из ответа на start_feed_assist
                # Если его нет (как в нашем случае), используем текущее значение из self._info
                result = response.get('result', {})
                initial_count_from_response = result.get('feed_assist_count', None) # <-- Получаем из ответа
                if initial_count_from_response is not None:
                    self._last_assist_count = initial_count_from_response
                    self.gcode.respond_info(f"// [ACE] _park_to_toolhead: Initial assist count from start response: {self._last_assist_count}") # Debug
                else:
                    # Если в ответе на start_feed_assist нет count, используем текущее значение из _info
                    # Это значение, которое было до отправки start_feed_assist (часто 0).
                    # Оно будет обновлено при первом изменении в _handle_response, когда status станет 'ready'.
                    current_count_from_info = self._info.get('feed_assist_count', 0)
                    self._last_assist_count = current_count_from_info
                    self.gcode.respond_info(f"// [ACE] _park_to_toolhead: No initial count in start response, using current from _info: {self._last_assist_count}") # Debug
                # self._assist_hit_count = 0 # <-- Сбрасывается при первом изменении в _handle_response
                # --- Конец изменений ---
                self._park_in_progress = True # <-- Теперь устанавливаем флаг
                self._park_index = index # <-- Убедимся, что индекс установлен
                # self._park_start_time = self.reactor.monotonic() # <-- Не нужен без таймера
                # self._start_park_timeout() # <-- Не нужен без таймера
                self.gcode.respond_info(f"// [ACE] _park_to_toolhead: Park started, tracking count: {self._last_assist_count}") # Debug
                # Теперь _handle_response начнёт получать статусы и отслеживать count в состоянии 'ready'
            except Exception as e:
                self.gcode.respond_raw(f"!! [ACE] _park_to_toolhead: Start assist callback error: {str(e)}") # Debug
                traceback.print_exc()
                self._complete_parking(success=False, error=f"Start callback error: {str(e)}")

        # Отправляем команду start_feed_assist
        self.gcode.respond_info(f"// [ACE] _park_to_toolhead: Sending start_feed_assist for slot {index}.") # Debug
        self.send_request({"method": "start_feed_assist", "params": {"index": index}}, start_assist_callback)

    def cmd_ACE_PARK_TO_TOOLHEAD(self, gcmd):
        if self._park_in_progress:
            gcmd.respond_raw("Already parking to toolhead")
            return
        g_index = gcmd.get_int('INDEX', minval=0, maxval=255)
        if g_index < self.tool_offset or g_index >= self.tool_offset + self.tool_slots:
            return
        index = g_index - self.tool_offset
        if self._info['slots'][index]['status'] != 'ready':
            self.gcode.run_script_from_command(f"_ACE_ON_EMPTY_ERROR INDEX={index}")
            return
        self._park_to_toolhead(index)
        
    def _wait_for_slot_ready(self, index, on_ready):
        start_time = self.reactor.monotonic()
        def check_func(eventtime):
            try:
                if self._info['slots'][index]['status'] == 'ready':
                    on_ready()
                    return self.reactor.NEVER
                if eventtime - start_time > self.SLOT_READY_TIMEOUT:
                    self.gcode.respond_raw(f"[ACE] Timeout waiting for slot {index}. Forcing proceed.")
                    on_ready()
                    return self.reactor.NEVER
                return eventtime + 0.5
            except Exception as e:
                self.gcode.respond_info(f"Error in _wait_for_slot_ready: {str(e)}")
                traceback.print_exc()
                return self.reactor.NEVER
        return check_func
    
    def _proceed_with_toolchange(self, tool, was, gcmd):
        self._park_to_toolhead(tool)
        def callback():
            try:
                self.gcode.run_script_from_command(f'_ACE_POST_TOOLCHANGE FROM={was} TO={tool}')
            except Exception as e:
                self.gcode.respond_info(f"[ACE] Error in _proceed_with_toolchange: {e}")
                traceback.print_exc()
        self.dwell(15.0, callback)
        
    def cmd_ACE_CHANGE_TOOL(self, gcmd):
        tool = gcmd.get_int('TOOL', minval=-1, maxval=255)
        if self._name.startswith("ace "):
            suffix = self._name[4:]
        else:
            suffix = self._name
        safe_suffix = ''.join(ch if ch.isalnum() else '_' for ch in suffix).lower()
        current_tool_var = f"{safe_suffix}_current_index"    
 #       current_tool_var = f"{self._make_cmd_suffix(self._name)}_current_index"
        was = self.variables.get(current_tool_var, -1)
        if was == tool:
            gcmd.respond_info(f"Tool already set to {tool}")
            return
        if tool != -1:
            if tool < self.tool_offset or tool >= self.tool_offset + self.tool_slots:
                return
        local_tool = -1 if tool == -1 else (tool - self.tool_offset)
        local_was = -1 if was == -1 else (was - self.tool_offset)
        if local_tool != -1 and self._info['slots'][local_tool]['status'] != 'ready':
            self.gcode.run_script_from_command(f"_ACE_ON_EMPTY_ERROR INDEX={local_tool}")
            return
        self.gcode.run_script_from_command(f"_ACE_PRE_TOOLCHANGE FROM={was} TO={tool}")
        self._park_is_toolchange = True
        self._park_previous_tool = local_was
        if self.toolhead is None:
            gcmd.respond_raw("Toolhead not ready")
            return
        self.toolhead.wait_moves()
        self.variables[current_tool_var] = tool
        self.gcode.run_script_from_command(f'SAVE_VARIABLE VARIABLE={current_tool_var} VALUE={tool}')
        def callback(response):
            try:
                if response.get('code', 0) != 0:
                    gcmd.respond_raw(f"ACE Error: {response.get('msg', 'Unknown error')}")
            except Exception as e:
                self.gcode.respond_info(f"Unwind callback error: {str(e)}")
        if local_was != -1:
            self.send_request({
                "method": "unwind_filament",
                "params": {
                    "index": local_was,
                    "length": self.toolchange_retract_length,
                    "speed": self.retract_speed
                }
            }, callback)
            self.dwell((self.toolchange_retract_length / self.retract_speed) + 0.1, None)
            self.dwell(1.0, None)
            if local_tool != -1:
                check_timer = self._wait_for_slot_ready(local_was, lambda: self._proceed_with_toolchange(local_tool, local_was, gcmd))
                self.reactor.register_timer(check_timer, self.reactor.NOW)
            else:
                self._proceed_with_toolchange(local_tool, local_was, gcmd)
        else:
            self._park_to_toolhead(local_tool)
def load_config(config):
    return ValgAce(config)
def load_config_prefix(config):
    return ValgAce(config)

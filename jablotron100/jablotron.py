import binascii
from concurrent.futures import ThreadPoolExecutor
from homeassistant import core
from homeassistant.const import (
	CONF_PASSWORD,
	EVENT_HOMEASSISTANT_STOP,
	STATE_ALARM_DISARMED,
	STATE_ALARM_ARMED_AWAY,
	STATE_ALARM_ARMED_NIGHT,
	STATE_ALARM_ARMING,
	STATE_ALARM_PENDING,
	STATE_ALARM_TRIGGERED,
	STATE_OFF,
	STATE_ON,
)
from homeassistant.helpers.entity import Entity
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional
from .const import (
	CONF_SERIAL_PORT,
	CONF_REQUIRE_CODE_TO_ARM,
	CONF_REQUIRE_CODE_TO_DISARM,
	DEFAULT_CONF_REQUIRE_CODE_TO_ARM,
	DEFAULT_CONF_REQUIRE_CODE_TO_DISARM,
	DOMAIN,
	LOGGER,
)
from .errors import (
	ModelNotDetected,
	ModelNotSupported,
	ServiceUnavailable,
	ShouldNotHappen,
)

MAX_WORKERS = 5
TIMEOUT = 10
PACKET_READ_SIZE = 64

JABLOTRON_MAX_SECTIONS = 16

# x02 model
# x08 hardware version
# x09 firmware version
# x0a registration code
# x0b name of the installation
JABLOTRON_PACKET_GET_INFO = b"\x30\x01\x02\x30\x01\x08\x30\x01\x09"
JABLOTRON_PACKET_GET_STATES = b"\x80\x01\x01\x52\x01\x0e"
JABLOTRON_PACKET_STATES_PREFIX = b"\x51\x22"
JABLOTRON_PACKET_INFO_PREFIX = b"\x40"
JABLOTRON_INFO_MODEL = b"\x02"
JABLOTRON_INFO_HARDWARE_VERSION = b"\x08"
JABLOTRON_INFO_FIRMWARE_VERSION = b"\x09"
JABLOTRON_INFO_REGISTRATION_CODE = b"\x0a"
JABLOTRON_INFO_INSTALLATION_NAME = b"\x0b"

JABLOTRON_ALARM_STATE_DISARMED = b"\x01"
JABLOTRON_ALARM_STATE_DISARMED_WITH_PROBLEM = b"\x21"
JABLOTRON_ALARM_STATE_ARMING_FULL = b"\x83"
JABLOTRON_ALARM_STATE_ARMING_PARTIALLY = b"\x82"
JABLOTRON_ALARM_STATE_ARMED_FULL = b"\x03"
JABLOTRON_ALARM_STATE_ARMED_FULL_WITH_PROBLEM = b"\x23"
JABLOTRON_ALARM_STATE_ARMED_PARTIALLY = b"\x02"
JABLOTRON_ALARM_STATE_ARMED_PARTIALLY_WITH_PROBLEM = b"\x22"
JABLOTRON_ALARM_STATE_PENDING_FULL = b"\x43"
JABLOTRON_ALARM_STATE_PENDING_PARTIALLY = b"\x42"
JABLOTRON_ALARM_STATE_TRIGGERED_FULL = b"\x1b"
JABLOTRON_ALARM_STATE_TRIGGERED_PARTIALLY = b"\x12"
JABLOTRON_ALARM_STATE_OFF = b"\x07"


def decode_info_bytes(value: bytes) -> str:
	info = ""

	for i in range(0, len(value) - 1):
		letter = value[i:(i + 1)]

		if letter == b"\x00":
			break

		info += letter.decode()

	return info


def check_serial_port(serial_port: str) -> None:
	stop_event = threading.Event()
	thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

	def reader_thread() -> Optional[str]:
		model = None

		stream = open(serial_port, "rb")

		try:
			while not stop_event.is_set():
				packet = stream.read(PACKET_READ_SIZE)
				LOGGER.debug(packet)

				if packet[:1] == JABLOTRON_PACKET_INFO_PREFIX and packet[2:3] == JABLOTRON_INFO_MODEL:
					model = decode_info_bytes(packet[3:])
					break
		finally:
			stream.close()

		return model

	def writer_thread() -> None:
		while not stop_event.is_set():
			stream = open(serial_port, "wb")

			stream.write(JABLOTRON_PACKET_GET_INFO)
			time.sleep(0.1)

			stream.close()

			time.sleep(1)

	try:
		reader = thread_pool_executor.submit(reader_thread)
		thread_pool_executor.submit(writer_thread)

		model = reader.result(TIMEOUT)

		if model is None:
			raise ModelNotDetected

		if not re.match(r"JA-101", model):
			raise ModelNotSupported("Model {} not supported".format(model))

	except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError):
		raise ServiceUnavailable

	finally:
		stop_event.set()
		thread_pool_executor.shutdown()


class JablotronCentralUnit:

	def __init__(self, serial_port: str, model: str, hardware_version: str, firmware_version: str):
		self.serial_port: str = serial_port
		self.model: str = model
		self.hardware_version: str = hardware_version
		self.firmware_version: str = firmware_version


class JablotronControl:

	def __init__(self, central_unit: JablotronCentralUnit, name: str, id: str):
		self.central_unit: JablotronCentralUnit = central_unit
		self.name: str = name
		self.id: str = id


class JablotronAlarmControlPanel(JablotronControl):

	def __init__(self, central_unit: JablotronCentralUnit, section: int, name: str, id: str):
		self.section: int = section

		super().__init__(central_unit, name, id)


class Jablotron():

	def __init__(self, hass: core.HomeAssistant, config: Dict[str, str], options: Dict[str, Any]) -> None:
		self._hass: core.HomeAssistant = hass
		self._config: Dict[str, str] = config
		self._options: Dict[str, Any] = options

		self._central_unit: Optional[JablotronCentralUnit] = None
		self._alarm_control_panels: List[JablotronAlarmControlPanel] = []
		self._section_problem_sensors: List[JablotronControl] = []

		self._entities: Dict[str, JablotronEntity] = {}

		self._state_checker_thread_pool_executor: Optional[ThreadPoolExecutor] = None
		self._state_checker_stop_event: threading.Event = threading.Event()
		self._state_checker_data_updating_event: threading.Event = threading.Event()

		self.states: Dict[str, str] = {}
		self.last_update_success: bool = False

	def update_options(self, options: Dict[str, Any]) -> None:
		self._options = options

		for entity in self._entities.values():
			entity.async_write_ha_state()

	def is_code_required_for_disarm(self) -> bool:
		return self._options.get(CONF_REQUIRE_CODE_TO_DISARM, DEFAULT_CONF_REQUIRE_CODE_TO_DISARM)

	def is_code_required_for_arm(self) -> bool:
		return self._options.get(CONF_REQUIRE_CODE_TO_ARM, DEFAULT_CONF_REQUIRE_CODE_TO_ARM)

	def initialize(self) -> None:
		def shutdown_event(_):
			self.shutdown()

		self._hass.bus.async_listen(EVENT_HOMEASSISTANT_STOP, shutdown_event)

		self._detect_central_unit()
		self._detect_sections()

		# Initialize states checker
		self._state_checker_thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
		self._state_checker_thread_pool_executor.submit(self._read_state)
		self._state_checker_thread_pool_executor.submit(self._get_states)

	def shutdown(self) -> None:
		self._state_checker_stop_event.set()

		# Send packet so read thread can finish
		self._send_packet(JABLOTRON_PACKET_GET_STATES)

		if self._state_checker_thread_pool_executor is not None:
			self._state_checker_thread_pool_executor.shutdown()

	def substribe_entity_for_updates(self, control_id: str, entity) -> None:
		self._entities[control_id] = entity

	def modify_alarm_control_panel_section_state(self, section: int, state: str, code: Optional[str]) -> None:
		if code is None:
			code = self._config[CONF_PASSWORD]

		int_packets = {
			STATE_ALARM_DISARMED: 143,
			STATE_ALARM_ARMED_AWAY: 159,
			STATE_ALARM_ARMED_NIGHT: 175,
		}

		state_packet = Jablotron._int_to_bytes(int_packets[state] + section)

		self._send_packet(Jablotron._create_code_packet(code) + b"\x80\x02\x0d" + state_packet)

	def alarm_control_panels(self) -> List[JablotronAlarmControlPanel]:
		return self._alarm_control_panels

	def section_problem_sensors(self) -> List[JablotronControl]:
		return self._section_problem_sensors

	def _detect_central_unit(self) -> None:
		stop_event = threading.Event()
		thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

		def reader_thread() -> Optional[JablotronCentralUnit]:
			model = None
			hardware_version = None
			firmware_version = None

			stream = open(self._config[CONF_SERIAL_PORT], "rb")

			try:
				while not stop_event.is_set():
					packet = stream.read(PACKET_READ_SIZE)

					if packet[:1] == JABLOTRON_PACKET_INFO_PREFIX:
						if packet[2:3] == JABLOTRON_INFO_MODEL:
							model = decode_info_bytes(packet[3:])
						elif packet[2:3] == JABLOTRON_INFO_HARDWARE_VERSION:
							hardware_version = decode_info_bytes(packet[3:])
						elif packet[2:3] == JABLOTRON_INFO_FIRMWARE_VERSION:
							firmware_version = decode_info_bytes(packet[3:])

					if model is not None and hardware_version is not None and firmware_version is not None:
						break
			finally:
				stream.close()

			if model is None or hardware_version is None or firmware_version is None:
				return None

			return JablotronCentralUnit(self._config[CONF_SERIAL_PORT], model, hardware_version, firmware_version)

		def writer_thread() -> None:
			while not stop_event.is_set():
				self._send_packet(JABLOTRON_PACKET_GET_INFO)
				time.sleep(1)

		try:
			reader = thread_pool_executor.submit(reader_thread)
			thread_pool_executor.submit(writer_thread)

			self._central_unit = reader.result(TIMEOUT)

		except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError) as ex:
			LOGGER.error(format(ex))
			raise ServiceUnavailable

		finally:
			stop_event.set()
			thread_pool_executor.shutdown()

		if self._central_unit is None:
			raise ShouldNotHappen

	def _detect_sections(self) -> None:
		stop_event = threading.Event()
		thread_pool_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

		def reader_thread() -> Optional[Dict[int, bytes]]:
			section_states = None

			stream = open(self._config[CONF_SERIAL_PORT], "rb")

			try:
				while not stop_event.is_set():
					packet = stream.read(PACKET_READ_SIZE)

					if packet[:2] == JABLOTRON_PACKET_STATES_PREFIX:
						section_states = self._parse_state_packet(packet)
						break
			finally:
				stream.close()

			if section_states is None:
				return None

			return section_states

		def writer_thread() -> None:
			while not stop_event.is_set():
				self._send_packet(JABLOTRON_PACKET_GET_STATES)
				time.sleep(1)

		try:
			reader = thread_pool_executor.submit(reader_thread)
			thread_pool_executor.submit(writer_thread)

			section_states = reader.result(TIMEOUT)

		except (IndexError, FileNotFoundError, IsADirectoryError, UnboundLocalError, OSError) as ex:
			LOGGER.error(format(ex))
			raise ServiceUnavailable

		finally:
			stop_event.set()
			thread_pool_executor.shutdown()

		if section_states is None:
			raise ShouldNotHappen

		for section, section_state in section_states.items():
			section_name = self._create_section_name(section)

			section_alarm_id = Jablotron._create_section_alarm_id(section)
			section_problem_sensor_id = Jablotron._create_section_problem_sensor_id(section)

			self._alarm_control_panels.append(JablotronAlarmControlPanel(
				self._central_unit,
				section,
				section_name,
				section_alarm_id,
			))
			self._section_problem_sensors.append(JablotronControl(
				self._central_unit,
				section_name,
				section_problem_sensor_id,
			))

			self.states[section_alarm_id] = Jablotron._convert_jablotron_alarm_state_to_alarm_state(section_state)
			self.states[section_problem_sensor_id] = Jablotron._convert_alarm_jablotron_alarm_state_to_problem_sensor_state(section_state)

		self.last_update_success = True

	def _parse_state_packet(self, packet: bytes) -> Dict[int, bytes]:
		section_states = {}

		for section in range(1, JABLOTRON_MAX_SECTIONS):
			state_offset = section * 2
			state = packet[state_offset:(state_offset + 1)]

			if state == JABLOTRON_ALARM_STATE_OFF:
				break

			section_states[section] = state

		return section_states

	def _read_state(self) -> None:
		def update_state(id: str, state: str) -> None:
			if id in self.states and state == self.states[id]:
				return

			self.states[id] = state

			if id in self._entities:
				self._entities[id].async_write_ha_state()

		while not self._state_checker_stop_event.is_set():
			stream = None

			try:
				stream = open(self._config[CONF_SERIAL_PORT], "rb")

				while True:

					self._state_checker_data_updating_event.clear()

					packet = stream.read(PACKET_READ_SIZE)
					# LOGGER.debug(str(binascii.hexlify(packet), "utf-8"))

					self._state_checker_data_updating_event.set()

					if not packet:
						self.last_update_success = False
						break

					if packet[:2] == JABLOTRON_PACKET_STATES_PREFIX:
						self.last_update_success = True

						section_states = self._parse_state_packet(packet)

						for section, section_state in section_states.items():
							update_state(
								Jablotron._create_section_alarm_id(section),
								Jablotron._convert_jablotron_alarm_state_to_alarm_state(section_state),
							)

							update_state(
								Jablotron._create_section_problem_sensor_id(section),
								Jablotron._convert_alarm_jablotron_alarm_state_to_problem_sensor_state(section_state),
							)

						break

			except Exception as ex:
				LOGGER.error(format(ex))
				self.last_update_success = False

			finally:
				if stream is not None:
					stream.close()

			time.sleep(0.5)

	def _get_states(self):
		counter = 0
		while not self._state_checker_stop_event.is_set():
			if counter == 0:
				if not self._state_checker_data_updating_event.wait(0.5):
					self._send_packet(JABLOTRON_PACKET_GET_STATES)

			time.sleep(1)
			counter += 1
			if counter == 30:
				counter = 0

	def _send_packet(self, packet) -> None:
		stream = open(self._config[CONF_SERIAL_PORT], "wb")

		stream.write(packet)
		time.sleep(0.1)

		stream.close()

	@staticmethod
	def _create_code_packet(code: str) -> bytes:
		code_packet = b""
		for code_number in code:
			code_packet += Jablotron._int_to_bytes(48 + int(code_number))

		return b"\x80\x08\x03\x39\x39\x39" + code_packet

	@staticmethod
	def _int_to_bytes(number: int) -> bytes:
		return int.to_bytes(number, 1, byteorder=sys.byteorder)

	@staticmethod
	def _create_section_name(section: int) -> str:
		return "Section {}".format(section)

	@staticmethod
	def _create_section_alarm_id(section: int) -> str:
		return "section_{}".format(section)

	@staticmethod
	def _create_section_problem_sensor_id(section: int) -> str:
		return "section_problem_sensor_{}".format(section)

	@staticmethod
	def _convert_jablotron_alarm_state_to_alarm_state(state: bytes) -> str:
		if state == JABLOTRON_ALARM_STATE_ARMED_FULL or state == JABLOTRON_ALARM_STATE_ARMED_FULL_WITH_PROBLEM:
			return STATE_ALARM_ARMED_AWAY

		if state == JABLOTRON_ALARM_STATE_ARMED_PARTIALLY or state == JABLOTRON_ALARM_STATE_ARMED_PARTIALLY_WITH_PROBLEM:
			return STATE_ALARM_ARMED_NIGHT

		if state == JABLOTRON_ALARM_STATE_ARMING_FULL or state == JABLOTRON_ALARM_STATE_ARMING_PARTIALLY:
			return STATE_ALARM_ARMING

		if state == JABLOTRON_ALARM_STATE_PENDING_FULL or state == JABLOTRON_ALARM_STATE_PENDING_PARTIALLY:
			return STATE_ALARM_PENDING

		if state == JABLOTRON_ALARM_STATE_TRIGGERED_FULL or state == JABLOTRON_ALARM_STATE_TRIGGERED_PARTIALLY:
			return STATE_ALARM_TRIGGERED

		return STATE_ALARM_DISARMED

	@staticmethod
	def _convert_alarm_jablotron_alarm_state_to_problem_sensor_state(state: bytes) -> str:
		if (
			state == JABLOTRON_ALARM_STATE_ARMED_FULL_WITH_PROBLEM
			or state == JABLOTRON_ALARM_STATE_ARMED_PARTIALLY_WITH_PROBLEM
			or state == JABLOTRON_ALARM_STATE_DISARMED_WITH_PROBLEM
		):
			return STATE_ON

		return STATE_OFF

class JablotronEntity(Entity):
	_state: str

	def __init__(
			self,
			jablotron: Jablotron,
			control: JablotronControl,
	) -> None:
		self._jablotron: Jablotron = jablotron
		self._control: JablotronControl = control

	@property
	def should_poll(self) -> bool:
		return False

	@property
	def available(self) -> bool:
		return self._jablotron.last_update_success

	@property
	def device_info(self) -> Dict[str, str]:
		return {
			"identifiers": {(DOMAIN, self._control.central_unit.serial_port)},
			"name": "Jablotron 100",
			"model": "{} ({})".format(self._control.central_unit.model, self._control.central_unit.hardware_version),
			"manufacturer": "Jablotron",
			"sw_version": self._control.central_unit.firmware_version,
		}

	@property
	def name(self) -> str:
		return self._control.name

	@property
	def unique_id(self) -> str:
		return "{}.{}.{}".format(DOMAIN, self._control.central_unit.serial_port, self._control.id)

	@property
	def state(self) -> str:
		return self._jablotron.states[self._control.id]

	async def async_added_to_hass(self) -> None:
		self._jablotron.substribe_entity_for_updates(self._control.id, self)

	def update_state(self, state: str) -> None:
		self._jablotron.states[self._control.id] = state
		self.async_write_ha_state()

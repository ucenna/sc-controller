"""
Universal driver for gamepads managed by evdev.

Handles no devices by default. Instead of trying to guess which evdev device
is a gamepad and which user actually wants to be handled by SCC, list of enabled
devices is read from config file.
"""

from scc.constants import STICK_PAD_MIN, STICK_PAD_MAX, TRIGGER_MAX
from scc.constants import SCButtons, ControllerFlags
from scc.controller import Controller
from scc.paths import get_config_path
from scc.config import Config
from scc.poller import Poller
from collections import namedtuple
import evdev
import struct, threading, Queue, os, time, binascii, json, logging
log = logging.getLogger("evdev")


EvdevControllerInput = namedtuple('EvdevControllerInput',
	'buttons ltrig rtrig stick_x stick_y lpad_x lpad_y rpad_x rpad_y'
)

AxisCalibrationData = namedtuple('AxisCalibrationData',
	'scale offset center'
)

class EvdevController(Controller):
	"""
	Wrapper around evdev device.
	To keep stuff simple, this class tries to provide and use same methods
	as SCController class does.
	"""
	
	def __init__(self, daemon, device, config):
		Controller.__init__(self)
		self.flags = ControllerFlags.HAS_RSTICK | ControllerFlags.SEPARATE_STICK
		self.device = device
		self.config = config
		self.poller = daemon.get_poller()
		self.poller.register(self.device.fd, self.poller.POLLIN, self.input)
		self.device.grab()
		self._id = self._generate_id()
		self._state = EvdevControllerInput( *[0] * len(EvdevControllerInput._fields) )
		self._parse_config(config)
	
	
	def _parse_config(self, config):
		self._evdev_to_button = {}
		self._evdev_to_axis = {}
		self._calibrations = {}
		
		for x, value in config.get("buttons", {}).iteritems():
			try:
				keycode = int(x)
				sc = getattr(SCButtons, value)
				self._evdev_to_button[keycode] = sc
			except: pass
		for x, value in config.get("axes", {}).iteritems():
			code, axis = int(x), value.get("axis")
			if axis in EvdevControllerInput._fields:
				min, max, center = (value.get("min", -127),
						value.get("max", 128), value.get("center", 0))
				if axis in ("ltrig", "rtrig"):
					if max > min:
						self._calibrations[code]= AxisCalibrationData(
							-2.0 / (min-max), -3.0, min)
					else:
						self._calibrations[code]= AxisCalibrationData(
							-2.0 / (min-max), 1.0, max)
				else:
					if max > min:
						self._calibrations[code]= AxisCalibrationData(
							-2.0 / (min-max), -1.0, center)
					else:
						self._calibrations[code]= AxisCalibrationData(
							-2.0 / (min-max), 1.0, center)
				self._evdev_to_axis[code] = axis
	
	
	def close(self):
		self.poller.unregister(self.device.fd)
		try:
			self.device.ungrab()
		except: pass
		self.device.close()
	
	
	def get_type(self):
		return "evdev"
	
	
	def get_id(self):
		return self._id
	
	
	def _generate_id(self):
		"""
		ID is generated as 'ev' + upper_case(hex(crc32(device name + X)))
		where 'X' starts as 0 and increases as controllers with same name are
		connected.
		"""
		magic_number = 0
		id = None
		while id is None or id in _evdevdrv._used_ids:
			crc32 = binascii.crc32("%s%s" % (self.device.name, magic_number))
			id = "ev%s" % (hex(crc32).upper().strip("-0X"),)
			magic_number += 1
		_evdevdrv._used_ids.add(id)
		return id
	
	
	def get_id_is_persistent(self):
		return True
	
	
	def __repr__(self):
		return "<Evdev %s>" % (self.device.name,)
	
	
	def input(self, *a):
		new_state = self._state
		try:
			for event in self.device.read():
				if event.type == evdev.ecodes.EV_KEY:
					if event.code in self._evdev_to_button:
						if event.value:
							b = new_state.buttons | self._evdev_to_button[event.code]
							new_state = new_state._replace(buttons=b)
						else:
							b = new_state.buttons & ~self._evdev_to_button[event.code]
							new_state = new_state._replace(buttons=b)
				elif event.type == evdev.ecodes.EV_ABS:
					if event.code in self._evdev_to_axis:
						cal = self._calibrations[event.code]
						value = (float(event.value) * cal.scale + cal.offset)
						value = int(value * STICK_PAD_MAX)
						if value >= -cal.center and value <= cal.center:
							value = 0
						new_state = new_state._replace(**{
							self._evdev_to_axis[event.code] : value
						})
		except IOError, e:
			# TODO: Maybe check e.errno to determine exact error
			# all of them are fatal for now
			log.error(e)
			_evdevdrv.device_removed(self.device)
		if new_state is not self._state:
			# Something got changed
			old_state, self._state = self._state, new_state
			if self.mapper:
				self.mapper.input(self, time.time(), old_state, new_state)
	
	
	def apply_config(self, config):
		# TODO: This?
		pass
	
	
	def disconnected(self):
		# TODO: This!
		pass
	
	
	# def configure(self, idle_timeout=None, enable_gyros=None, led_level=None):
	
	
	def set_led_level(self, level):
		# TODO: This?
		pass
	
	
	def set_gyro_enabled(self, enabled):
		# TODO: This, maybe.
		pass
	
	
	def turnoff(self):
		"""
		Exists to stay compatibile with SCController class as evdev controller
		typically cannot be shut down like this.
		"""
		pass
	
	
	def get_gyro_enabled(self):
		""" Returns True if gyroscope input is currently enabled """
		return False
	
	
	def feedback(self, data):
		""" TODO: It would be nice to have feedback... """
		pass


class EvdevDriver(object):
	SCAN_INTERVAL = 5
	
	def __init__(self):
		self._daemon = None
		self._devices = {}
		self._new_devices = Queue.Queue()
		self._lock = threading.Lock()
		self._scan_thread = None
		self._used_ids = set()
		self._next_scan = None
	
	
	def handle_new_device(self, dev, config):
		controller = EvdevController(self._daemon, dev, config)
		self._devices[dev.fn] = controller
		self._daemon.add_controller(controller)
		log.debug("Evdev device added: %s", dev.name)
	
	
	def device_removed(self, dev):
		if dev.fn in self._devices:
			controller = self._devices[dev.fn]
			del self._devices[dev.fn]
			self._daemon.remove_controller(controller)
			self._used_ids.remove(controller.get_id())
			controller.close()
	
	
	def scan(self):
		# Scanning is slow, so it runs in thread
		with self._lock:
			if self._scan_thread is None:
				self._scan_thread = threading.Thread(
						target = self._scan_thread_target)
				self._scan_thread.start()
	
	
	def _scan_thread_target(self):
		c = Config()
		for fname in evdev.list_devices():
			dev = evdev.InputDevice(fname)
			if dev.fn not in self._devices:
				config_file = os.path.join(get_config_path(), "devices",
					"%s.json" % (dev.name.strip(),))
				if os.path.exists(config_file):
					config = None
					try:
						config = json.loads(open(config_file, "r").read())
						with self._lock:
							self._new_devices.put(( dev, config ))
					except Exception, e:
						log.exception(e)
		with self._lock:
			self._scan_thread = None
			self._next_scan = time.time() + EvdevDriver.SCAN_INTERVAL
	
	
	def start(self):
		self.scan()
	
	
	def mainloop(self):
		if time.time() > self._next_scan:
			self.scan()
		with self._lock:
			while not self._new_devices.empty():
				dev, config = self._new_devices.get()
				if dev.fn not in self._devices:
					self.handle_new_device(dev, config)

# Just like USB driver, EvdevDriver is process-wide singleton
_evdevdrv = EvdevDriver()

def init(daemon):
	_evdevdrv._daemon = daemon
	daemon.add_mainloop(_evdevdrv.mainloop)
	# daemon.on_daemon_exit(_evdevdrv.on_exit)

def start(daemon):
	_evdevdrv.start()

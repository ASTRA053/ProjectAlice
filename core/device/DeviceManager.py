import json
import socket
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import esptool
import requests
import serial
from esptool import ESPLoader
from paho.mqtt.client import MQTTMessage
from random import shuffle
from serial.tools import list_ports

from core.base.model.Manager import Manager
from core.commons import constants
from core.device.model.Device import Device
from core.device.model.DeviceType import DeviceType
from core.device.model.TasmotaConfigs import TasmotaConfigs
from core.dialog.model.DialogSession import DialogSession


class DeviceManager(Manager):
#todo remove all room: string - replace with locationID

	DB_DEVICE = 'devices'
	DB_LINKS = 'deviceLinks'
	DB_TYPES = 'deviceTypes'
	DATABASE = {
		DB_DEVICE: [
			'id INTEGER PRIMARY KEY',
			'typeID INTEGER NOT NULL',
			'uid TEXT',
			'locationID INTEGER NOT NULL',
			'name TEXT',
			'display TEXT',
			'devSettings TEXT'
		],
		DB_LINKS: [
			'id INTEGER PRIMARY KEY',
			'deviceID INTEGER NOT NULL',
			'locationID INTEGER NOT NULL',
			'locSettings TEXT'
		],
		DB_TYPES: [
			'id INTEGER PRIMARY KEY',
			'skill TEXT NOT NULL',
			'name TEXT NOT NULL',
			'devSettings TEXT',
			'locSettings TEXT'
		]
	}


	def __init__(self):
		super().__init__(databaseSchema=self.DATABASE)

		self._devices: Dict[str, Device] = dict()
		#self._idToUID: Dict[int, str] = dict() #todo maybe relevant for faster access?
		self._deviceTypes: Dict[str, DeviceType] = dict()
		self._broadcastRoom = ''
		self._broadcastFlag = threading.Event()

		self._broadcastPort = None
		self._broadcastTimer = None

		self._flashThread = None

		self._listenPort = None

		self._broadcastSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
		self._broadcastSocket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
		self._broadcastSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

		self._listenSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
		self._listenSocket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
		self._listenSocket.settimeout(2)

		self._heartbeats = dict()
		self._heartbeatsCheckTimer = None
		self._heartbeatTimer = None


	def onStart(self):
		super().onStart()

		self._broadcastPort = int(self.ConfigManager.getAliceConfigByName('newDeviceBroadcastPort'))  # Default 12354
		self._listenPort = self._broadcastPort + 1

		self._listenSocket.bind(('', self._listenPort))

		self.loadDevices()

		self.logInfo(f'Loaded **{len(self._devices)}** device', plural='device')


	@property
	def broadcastRoom(self) -> str:
		return self._broadcastRoom

	@property
	def devices(self) -> Dict[str, Device]:
		return self._devices


	def onBooted(self):
		self.MqttManager.publish(topic='projectalice/devices/coreReconnection')

		if self._devices:
			self._heartbeatsCheckTimer = self.ThreadManager.newTimer(interval=3, func=self.checkHeartbeats)
			self._heartbeatTimer = self.ThreadManager.newTimer(interval=2, func=self.sendHeartbeat)


	def onStop(self):
		super().onStop()
		self.stopBroadcasting()
		self._broadcastSocket.close()
		self.MqttManager.publish(topic='projectalice/devices/coreDisconnection')


	def deviceMessage(self, message: MQTTMessage) -> DialogSession:
		return self.DialogManager.newTempSession(message=message)


	def loadDevices(self):
		for row in self.databaseFetch(tableName=self.DB_DEVICE, method='all'):
			# to dict!
			self._devices[row['id']] = Device(row)


	# noinspection SqlResolve
	def isUIDAvailable(self, uid: str) -> bool:
		try:
			count = self.databaseFetch(tableName='devices', query='SELECT COUNT() FROM :__table__ WHERE uid = :uid', values={'uid': uid})[0]
			return count <= 0
		except sqlite3.OperationalError as e:
			self.logWarning(f"Couldn't check device from database: {e}")
			return False


	# noinspection SqlResolve
	def addNewDevice(self, deviceTypeID: int, locationID: int = None, room: str = None, uid: str = None) -> Device:
		# get or create location from different inputs
		try:
			if not room and not locationID:
				room = self._broadcastRoom

			location = self.LocationManager.getLocation(id=locationID, room=room)

			self.assertDeviceTypeAllowedAtLocation(locationID=location.id, deviceTypeID=deviceTypeID)

			values = {'typeID': deviceTypeID, 'uid': uid, 'locationID': location.id, 'display': "{}"}
			values['id'] = self.databaseInsert(tableName=self.DB_DEVICE, values=values)

			self._devices[values['id']] = Device(data=values)
			return self._devices[values['id']]
		except Exception as e:
			self.logWarning(f"Couldn't insert device in database: {e}")
			return None


	def devUIDtoID(self, UID: str) -> int:
		return self.devices[UID].id


	def devIDtoUID(self, ID: int) -> str:
		for uid, dev in self.devices:
			if dev.id == ID:
				return uid


	def deleteDeviceID(self, deviceID: int):
		self.devices.pop(deviceID)
		self.DatabaseManager.delete(tableName='devices', callerName=self.name, query='DELETE :__table__ WHERE id = :id', values={id: deviceID})
		self.DatabaseManager.delete(tableName='deviceLinks', callerName=self.name, query='DELETE :__table__ WHERE id = :id', values={id: deviceID})


	def getDeviceType(self, id):
		return self.deviceTypes[id]


	def getDeviceTypesForSkill(self, skillName: str) -> Dict[int, DeviceType]:
		return {id: deviceType for id, deviceType in self.deviceTypes.items() if deviceType.skill == skillName}


	def removeDeviceTypesForSkill(self,skillName: str):
		for id, deviceType in self.getDeviceTypesForSkill(skillName):
			self.deviceTypes.pop(id, None)


	def addDeviceTypes(self, deviceTypes: Dict):
		self.deviceTypes.update(deviceTypes)


	def removeDeviceType(self, id: int):
		self.DatabaseManager.delete(
			tableName=self.DB_TYPES,
			callerName=self.name,
			values={
				'id': id
			}
		)
		self._deviceTypes.pop(id)


	def addLink(self, id: int, locationid: int):
		device = self.getDeviceByID(id)
		deviceType = device.getDeviceType()
		if not deviceType.canHaveMultipleRooms():
			raise Exception(f'Device type {deviceType.name} can\'t be linked to other rooms')

		#todo get device/room specific settings
		locSettings = []
		values = {'id' : id, 'locationid': locationid, 'locSettings': locSettings}
		self.databaseInsert(tableName=self.DB_LINKS, query='INSERT INTO :__table__ (deviceID, locationID, locSettings) VALUES (:id, :locationid, locSettings)', values=values)


	def deleteDeviceUID(self, deviceUID: str):
		self.deleteDeviceID(deviceID=devUIDtoID(UID=deviceUID))


	def _getFreeUID(self, base: str = '') -> str:
		"""
		Gets a free uid. A free uid is a uid not declared in database. If base is provided it will be used as a uid pattern
		:param base: str
		:return: str
		"""
		if not base:
			uid = str(uuid.uuid4())
		else:
			uid = base = base.replace(':', '').replace(' ', '')

		while not self.isUIDAvailable(uid):
			if not base:
				uid = str(uuid.uuid4())
			else:
				aList = list(base)
				shuffle(aList)
				uid = ''.join(aList)

		return uid

## Braodcasting ## todo move to SAT skill?
	@property
	def broadcastFlag(self) -> threading.Event:
		return self._broadcastFlag


	def startBroadcastingForNewDevice(self, room: str, siteId: str, uid: str = '') -> bool:
		if self.isBusy():
			return False

		self._broadcastRoom = self.Commons.cleanRoomNameToSiteId(room)

		if not uid:
			uid = self._getFreeUID()

		self.logInfo(f'Started broadcasting on {self._broadcastPort} for new device addition. Attributed uid: {uid}')
		self._listenSocket.listen(2)
		self.ThreadManager.newThread(name='broadcast', target=self.startBroadcast, args=[room, uid, siteId])

		self._broadcastTimer = self.ThreadManager.newTimer(interval=300, func=self.stopBroadcasting)

		self.broadcast(method=constants.EVENT_BROADCASTING_FOR_NEW_DEVICE, exceptions=[self.name], propagateToSkills=True)
		return True


	def stopBroadcasting(self):
		self.logInfo('Stopped broadcasting for new devices')
		self._broadcastFlag.clear()

		if self._broadcastTimer:
			self._broadcastTimer.cancel()

		self._broadcastRoom = ''
		self.broadcast(method=constants.EVENT_STOP_BROADCASTING_FOR_NEW_DEVICE, exceptions=[self.name], propagateToSkills=True)


	def startBroadcast(self, room: str, uid: str, replyOnSiteId: str):
		self._broadcastFlag.set()
		while self._broadcastFlag.isSet():
			self._broadcastSocket.sendto(bytes(f'{self.Commons.getLocalIp()}:{self._listenPort}:{room.replace(" ", "_")}:{uid}', encoding='utf8'), ('<broadcast>', self._broadcastPort))
			try:
				sock, address = self._listenSocket.accept()
				sock.settimeout(None)
				answer = sock.recv(1024).decode()

				deviceIp = answer.split(':')[0]
				deviceType = answer.split(':')[1]

				if deviceType.lower() == 'alicesatellite':
					for satellite in self.getDevicesByRoom(room):
						if satellite.deviceType.lower() == 'alicesatellite':
							self.logWarning('Cannot have more than one Alice skill per room, aborting')
							self.MqttManager.say(text=self.TalkManager.randomTalk('maxOneAlicePerRoom', skill='system'), client=replyOnSiteId)
							answer = 'nok'
							break

				if answer != 'nok':
					if self.addNewDevice(deviceType, room, uid):
						self.logInfo(f'New device with uid {uid} successfully added')
						self.MqttManager.say(text=self.TalkManager.randomTalk('newDeviceAdditionSuccess', skill='system'), client=replyOnSiteId)
						answer = 'ok'
					else:
						self.logInfo('Failed adding new device')
						self.MqttManager.say(text=self.TalkManager.randomTalk('newDeviceAdditionFailed', skill='system'), client=replyOnSiteId)
						answer = 'nok'

					if deviceType.lower() == 'alicesatellite':
						self.ThreadManager.doLater(interval=5, func=self.WakewordManager.uploadToNewDevice, args=[uid])

				self._broadcastSocket.sendto(bytes(answer, encoding='utf8'), (deviceIp, self._broadcastPort))
				self.stopBroadcasting()
			except socket.timeout:
				self.logInfo('No device query received')


	def broadcastToDevices(self, topic: str, payload: dict = None, deviceType: str = None, room: str = None, connectedOnly: bool = True):
		if not payload:
			payload = dict()

		for device in self._devices.values():
			if deviceType and device.deviceType.lower() != deviceType.lower():
				continue

			if room and device.room.lower() != room.lower():
				continue

			if connectedOnly and not device.connected:
				continue

			payload.setdefault('uid', device.uid)
			payload.setdefault('siteId', device.room)

			self.MqttManager.publish(
				topic=topic,
				payload=json.dumps(payload)
			)


	def deviceConnecting(self, uid: str) -> Optional[Device]:
		device = self.getDeviceByUID(uid)
		if not device:
			self.logWarning(f'A device with uid **{uid}** tried to connect but is unknown')
			return None

		if not device.connected:
			device.connected = True
			self.broadcast(method=constants.EVENT_DEVICE_CONNECTING, exceptions=[self.name], propagateToSkills=True)

		self._heartbeats[uid] = time.time() + 5
		if not self._heartbeatsCheckTimer:
			self._heartbeatsCheckTimer = self.ThreadManager.newTimer(interval=3, func=self.checkHeartbeats)

		return device


	def deviceDisconnecting(self, uid: str):
		self._heartbeats.pop(uid, None)

		device = self.getDeviceByUID(uid)

		if not device:
			return

		if device.connected:
			self.logInfo(f'Device with uid **{uid}** disconnected')
			device.connected = False
			self.broadcast(method=constants.EVENT_DEVICE_DISCONNECTING, exceptions=[self.name], propagateToSkills=True)


	def isBusy(self) -> bool:
		return self.ThreadManager.isThreadAlive('broadcast')

## Heartbeats
	def onDeviceHeartbeat(self, uid: str, siteId: str = None):
		device = self.getDeviceByUID(uid=uid)
		if not device:
			self.logWarning(f'Device with uid **{uid}** does not exist')
			return

		if siteId and siteId.replace(' ', '_').lower() != device.room.lower():
			self.logWarning(f'Device with uid **{uid}** is not matching its defined room (received **{siteId.replace(" ", "_").lower()}** but required **{device.room}**')
			return

		self._heartbeats[uid] = time.time()


	def checkHeartbeats(self):
		now = time.time()
		for uid, lastTime in self._heartbeats.copy().items():
			if now - 5 > lastTime:
				self.logWarning(f'Device with uid **{uid}** has not given a signal since 5 seconds or more')
				self._heartbeats.pop(uid)
				device = self.getDeviceByUID(uid)
				if device:
					device.connected = False

		self._heartbeatsCheckTimer = self.ThreadManager.newTimer(interval=3, func=self.checkHeartbeats)


	def sendHeartbeat(self):
		self.MqttManager.publish(
			topic=constants.TOPIC_CORE_HEARTBEAT
		)

		self._heartbeatTimer = self.ThreadManager.newTimer(interval=2, func=self.sendHeartbeat)

## base
	def getDeviceTypeBySkillRAW(self, skill: str):
		data = self.DatabaseManager.fetch(
			tableName=self.DB_TYPES,
			query='SELECT * FROM :__table__ WHERE skill = :skill',
			callerName=self.name,
			values={'skill': skill},
			method='all'
		)
		return {d['name']: {'id': d['id'], 'name': d['name'], 'skill': d['skill']} for d in data}


	def updateDevice(self,device: dict):
		self.getDeviceByID(device['id']).display = device['display']
		self.DatabaseManager.update(tableName=self.DB_DEVICE,
		                            callerName=self.name,
		                            values={'display': device['display']},
		                            row=('id', device['id']))


	def assertDeviceTypeAllowedAtLocation(self, typeID: int, locationID: int):
		# check max allowed per Location
		deviceType = self.getDeviceType(typeID)
		if deviceType.locationLimit > 0:
			if deviceType.locationLimit <= self.LocationManager.getDevicesByRoom(locationID=locationID,deviceTypeID=typeID):
				raise Exception(f'Maximum limit of devices in this location reached')


	@property
	def deviceTypes(self) -> dict:
		return self._deviceTypes


	def getDevicesByRoom(self, room: str = None, locationID: int = None, connectedOnly: bool = False, deviceTypeID: int = None) -> List[Device]:
		if not room and not locationID:
			raise Exception(f'you need to provice a room name or ID')

		if not locationID and room:
			locationID = self.LocationManager.getLocationWithName(room=room)

		return [device for device in self._devices.values() if device.locationID == locationID
		        and (not connectedOnly or device.connected)
		        and (not deviceTypeID or device.deviceTypeID == deviceTypeID)]


	def getDevicesByType(self, deviceType: int, connectedOnly: bool = False) -> List[Device]:
		return [x for x in self._devices.values() if x.deviceTypeID == deviceType and (not connectedOnly or x.connected)]


	def getDeviceByUID(self, uid: str) -> Optional[Device]:
		return self._devices.get(self.devUIDtoID(uid), None)


	def getDeviceByID(self, id: int) -> Optional[Device]:
		return self._devices.get(id, None)

## todo move to tasmota skill

	def doFlashTasmota(self, room: str, espType: str, siteId: str):
		port = self.findUSBPort(timeout=60)
		if not port:
			self.MqttManager.say(text=self.TalkManager.randomTalk('noESPFound', skill='AliceCore'), client=siteId)
			self._broadcastFlag.clear()
			return

		self.MqttManager.say(text=self.TalkManager.randomTalk('usbDeviceFound', skill='AliceCore'), client=siteId)
		try:
			mac = ESPLoader.detect_chip(port=port, baud=115200).read_mac()
			mac = ':'.join([f'{x:02x}' for x in mac])
			cmd = [
				'--port', port,
				'--baud', '115200',
				'--after', 'no_reset', 'write_flash',
				'--flash_mode', 'dout', '0x00000', 'tasmota.bin',
				'--erase-all'
			]

			esptool.main(cmd)
		except Exception as e:
			self.logError(f'Something went wrong flashing esp device: {e}')
			self.MqttManager.say(text=self.TalkManager.randomTalk('espFailed', skill='AliceCore'), client=siteId)
			self._broadcastFlag.clear()
			return

		self.logInfo('Tasmota flash done')
		self.MqttManager.say(text=self.TalkManager.randomTalk('espFlashedUnplugReplug', skill='AliceCore'), client=siteId)
		found = self.findUSBPort(timeout=60)
		if found:
			self.MqttManager.say(text=self.TalkManager.randomTalk('espFoundReadyForConf', skill='AliceCore'), client=siteId)
			time.sleep(10)
			uid = self._getFreeUID(mac)
			tasmotaConfigs = TasmotaConfigs(deviceType=espType, uid=uid)
			confs = tasmotaConfigs.getBacklogConfigs(room)
			if not confs:
				self.logError('Something went wrong getting tasmota configuration')
				self.MqttManager.say(text=self.TalkManager.randomTalk('espFailed', skill='AliceCore'), client=siteId)
			else:
				ser = serial.Serial()
				ser.baudrate = 115200
				ser.port = port
				ser.open()

				try:
					for group in confs:
						cmd = ';'.join(group['cmds'])
						if len(group['cmds']) > 1:
							cmd = f'Backlog {cmd}'

						arr = list()
						if len(cmd) > 50:
							while len(cmd) > 50:
								arr.append(cmd[:50])
								cmd = cmd[50:]
							arr.append(f'{cmd}\r\n')
						else:
							arr.append(f'{cmd}\r\n')

						for piece in arr:
							ser.write(piece.encode())
							self.logInfo('Sent {}'.format(piece.replace('\r\n', '')))
							time.sleep(0.5)

						time.sleep(group['waitAfter'])

					ser.close()
					self.logInfo('Tasmota flashing and configuring done')
					self.MqttManager.say(text=self.TalkManager.randomTalk('espFlashingDone', skill='AliceCore'), client=siteId)
					self.addNewDevice(espType, room, uid)
					self._broadcastFlag.clear()

				except Exception as e:
					self.logError(f'Something went wrong writting configuration to esp device: {e}')
					self.MqttManager.say(text=self.TalkManager.randomTalk('espFailed', skill='AliceCore'), client=siteId)
					self._broadcastFlag.clear()
					ser.close()
		else:
			self.MqttManager.say(text=self.TalkManager.randomTalk('espFailed', skill='AliceCore'), client=siteId)
			self._broadcastFlag.clear()


	def findUSBPort(self, timeout: int) -> str:
		oldPorts = list()
		scanPresent = True
		found = False
		tries = 0
		self.logInfo(f'Looking for USB device for the next {timeout} seconds')
		while not found:
			tries += 1
			if tries > timeout * 2:
				break

			newPorts = list()
			for port, desc, hwid in sorted(list_ports.comports()):
				if scanPresent:
					oldPorts.append(port)
				newPorts.append(port)

			scanPresent = False

			if len(newPorts) < len(oldPorts):
				# User disconnected a device
				self.logInfo('USB device disconnected')
				oldPorts = list()
				scanPresent = True
			else:
				changes = [port for port in newPorts if port not in oldPorts]
				if changes:
					port = changes[0]
					self.logInfo(f'Found usb device on {port}')
					return port

			time.sleep(0.5)

		return ''


	def startTasmotaFlashingProcess(self, room: str, espType: str, session: DialogSession) -> bool:
		self.ThreadManager.doLater(interval=0.5, func=self.MqttManager.endDialog, args=[session.sessionId, self.TalkManager.randomTalk('connectESPForFlashing', skill='AliceCore')])

		self._broadcastFlag.set()

		binFile = Path('tasmota.bin')
		if binFile.exists():
			binFile.unlink()

		try:
			req = requests.get(f'https://github.com/arendst/Tasmota/releases/download/v8.2.0/tasmota.bin')
			with binFile.open('wb') as file:
				file.write(req.content)
				self.logInfo('Downloaded tasmota.bin')
		except Exception as e:
			self.logError(f'Something went wrong downloading tasmota.bin: {e}')
			self._broadcastFlag.clear()
			return False

		self.ThreadManager.newThread(name='flashThread', target=self.doFlashTasmota, args=[room, espType, session.siteId])
		return True

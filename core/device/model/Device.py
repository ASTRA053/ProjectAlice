from dataclasses import dataclass, field

@dataclass
class Device:
	data: dict
	connected: bool = False
	name: str = ''
	lastContact: int = 0

	id: int = field(init=False)
	deviceType: str = field(init=False)
	uid: str = field(init=False)
	room: str = field(init=False)

	def __post_init__(self):
		self.id = self.data['id']
		self.deviceType = self.data['type']
		self.uid = self.data['uid']
		self.room = self.data['room']

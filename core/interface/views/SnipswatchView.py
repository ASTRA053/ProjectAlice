import subprocess
import tempfile

from flask import jsonify, render_template

from core.base.SuperManager import SuperManager
from core.interface.views.View import View


class SnipswatchView(View):

	def __init__(self):
		super().__init__()
		self._counter = 0
		self._thread = None
		self._file = tempfile.TemporaryFile(mode='a+')
		self._thread = None


	def index(self):
		return render_template('snipswatch.html', langData=self._langData)


	def startWatching(self):
		process = subprocess.Popen('snips-watch -vv --html', shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

		flag = SuperManager.getInstance().threadManager.newEvent('running')
		flag.set()
		while flag.isSet():
			out = process.stdout.readline().decode()
			if out:
				line = out.replace('<b><font color=#009900>', '<b><font color="green">').replace('#009900', '"yellow"').replace('#0000ff', '"green"')
				self._file.write(line)


	def update(self):
		return jsonify(data=self._getData())


	def refresh(self):
		if not self._thread:
			self._thread = SuperManager.getInstance().threadManager.newThread(
				name='snipswatch',
				target=self.startWatching,
				autostart=True
			)

		self._counter = 0
		return self.update()


	def _getData(self) -> list:
		try:
			data = self._file.readlines()
			ret = data[self._counter:]
			self._counter = len(data)
			return ret
		except:
			return list()

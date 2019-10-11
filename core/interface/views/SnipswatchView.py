import os
import subprocess
from pathlib import Path

import tempfile

from flask import jsonify, render_template, request
from flask_classful import route

from core.base.SuperManager import SuperManager
from core.interface.views.View import View


class SnipswatchView(View):
	route_base = '/snipswatch/'


	def __init__(self):
		super().__init__()
		self._counter = 0
		self._thread = None
		self._file = Path(tempfile.gettempdir(), 'snipswatch')
		self._process = None


	def index(self):
		self.newProcess()
		return render_template('snipswatch.html', langData=self._langData)


	def newProcess(self, verbosity: int = 2):
		SuperManager.getInstance().threadManager.getEvent('snipswatchrunning').clear()
		self._counter = 0
		if self._file.exists():
			os.remove(self._file)

		arg = ' -' + verbosity * 'v' if verbosity > 0 else ''

		if self._process is not None:
			self._process.kill()

		self._process = subprocess.Popen(f'snips-watch {arg} --html', shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
		self._thread = SuperManager.getInstance().threadManager.newThread(
			name='snipswatch',
			target=self.startWatching,
			autostart=True
		)


	def startWatching(self):
		flag = SuperManager.getInstance().threadManager.newEvent('snipswatchrunning')
		flag.set()
		while flag.isSet():
			out = self._process.stdout.readline().decode()
			if out:
				with open(self._file, 'a+') as fp:
					line = out.replace('<b><font color=#009900>', '<b><font color="green">').replace('#009900', '"yellow"').replace('#0000ff', '"green"')
					fp.write(line)


	@route('/refreshConsole', methods=['POST'])
	def refreshConsole(self):
		return jsonify(data=self._getData())


	@route('/verbosity', methods=['POST'])
	def verbosity(self):
		try:
			if self._process:
				self._process.terminate()

			verbosity = int(request.form.get('verbosity'))
			self.newProcess(verbosity=verbosity)

			return self.refreshConsole()
		except Exception as e:
			self._logger.error(f'[SnipswatchView] Error setting verbosity: {e}')
			return jsonify(success=False)


	def _getData(self) -> list:
		try:
			data = self._file.open('r').readlines()
			ret = data[self._counter:]
			self._counter = len(data)
			return ret
		except:
			return list()
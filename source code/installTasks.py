# -*- coding: UTF-8 -*-

import os
import shutil
import globalVars
import re

def onInstall():
	path = os.path.join(globalVars.appArgs.configPath, r'addons\googleTranslate\globalPlugins')
	if os.path.exists(path):
		for file in os.listdir(path):
			if re.match(r'transCache [a-zA-Z_]+ .json', file):
				shutil.copyfile(os.path.join(path, file), os.path.join(globalVars.appArgs.configPath, file))
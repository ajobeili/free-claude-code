"""Start the resident sender with Hermes' selected managed environment."""

import os
import sys
from pathlib import Path

from pm.environments import activation_environment, project_python

root = Path('/opt/hermes/app').resolve()
python = str(project_python(root))
environment = activation_environment(root)
environment['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
environment['PYTHONPATH'] = os.pathsep.join(
    value for value in (str(root), environment.get('PYTHONPATH', '')) if value
)
os.execve(
    python,
    [
        python,
        '-c',
        'from pathlib import Path; '
        'from pm.environments import activate_dependencies; '
        'activate_dependencies(Path("/opt/hermes/app").resolve()); '
        'import runpy,sys; '
        'sys.argv[0]="/opt/hermes/hermes_delivery_worker.py"; '
        'runpy.run_path(sys.argv[0],run_name="__main__")',
        *sys.argv[1:],
    ],
    environment,
)

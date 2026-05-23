from venv import create
from os.path import join, abspath, dirname
from subprocess import run

dirname = dirname(abspath(__file__))
venv_path = join(dirname, "backend", ".venv")

create(venv_path, with_pip=True, system_site_packages=True)
run(
    f". {join(venv_path, 'bin', 'activate')} && pip install -r {join(dirname, 'backend', 'requirements.txt')}",
    start_new_session=True,
    shell=True,
)

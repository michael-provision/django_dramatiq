import argparse
import importlib
import multiprocessing
import os
import pkgutil
import subprocess
import sys

from django.apps import apps
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils.module_loading import module_has_submodule

from django_dramatiq.utils import getenv_int


# Number of processes to use. Default: one per CPU.
NPROCS = getenv_int("DRAMATIQ_NPROCS", default=multiprocessing.cpu_count)

# Number of threads per process to use. Default: 8.
NTHREADS = getenv_int("DRAMATIQ_NTHREADS", 8)


class Command(BaseCommand):
    help = "Runs Dramatiq workers."

    def add_arguments(self, parser):
        parser.formatter_class = argparse.ArgumentDefaultsHelpFormatter
        parser.add_argument(
            "--skip-logging",
            action="store_true",
            dest="skip_logging",
            help="Do not call logging.basicConfig()"
        )
        watch_group = parser.add_mutually_exclusive_group()
        watch_group.add_argument(
            "--reload",
            action="store_const",
            const=".",
            dest="watch_dir",
            help="Enable autoreload. Equivalent to '--watch .'",
        )
        watch_group.add_argument(
            "--watch",
            dest="watch_dir",
            help="Reload workers when changes are detected in the given directory",
        )
        parser.add_argument(
            "--reload-use-polling",
            action="store_true",
            dest="use_polling_watcher",
            help=(
                "Use a poll-based file watcher for autoreload (useful under "
                "Vagrant and Docker for Mac)"
            ),
        )
        parser.add_argument(
            "--use-gevent",
            action="store_true",
            help="Use gevent for worker concurrency",
        )
        parser.add_argument(
            "--processes", "-p",
            default=NPROCS,
            type=int,
            help="The number of processes to run",
        )
        parser.add_argument(
            "--threads", "-t",
            default=NTHREADS,
            type=int,
            help="The number of threads per process to use",
        )
        parser.add_argument(
            "--path", "-P",
            default=".",
            nargs="*",
            type=str,
            help="The import path",
        )
        parser.add_argument(
            "--queues", "-Q",
            nargs="*",
            type=str,
            help="Listen to a subset of queues, or all when empty",
        )
        parser.add_argument(
            "--pid-file",
            type=str,
            help="Write the PID of the master process to this file",
        )
        parser.add_argument(
            "--log-file",
            type=str,
            help="Write all logs to a file, or stderr when empty",
        )
        parser.add_argument(
            "--fork-function",
            action="append", dest="forks", default=[],
            help="Fork a subprocess to run the given function",
        )
        parser.add_argument(
            "--worker-shutdown-timeout", type=int, default=600000,
            help="Timeout for worker shutdown, in milliseconds"
        )

    def handle(self, watch_dir, skip_logging, use_polling_watcher, use_gevent, path, processes, threads, verbosity,
               queues, pid_file, log_file, forks, worker_shutdown_timeout, **options):
        executable_name = "dramatiq-gevent" if use_gevent else "dramatiq"
        executable_path = self._resolve_executable(executable_name)
        watch_args = ["--watch", watch_dir] if watch_dir else []
        if watch_args and use_polling_watcher:
            watch_args.append("--watch-use-polling")

        forks_args = []
        if forks:
            for function in forks:
                forks_args += ["--fork-function", function]

        verbosity_args = ["-v"] * (verbosity - 1)
        tasks_modules = self.discover_tasks_modules()
        process_args = [
            executable_name,
            "--path", *path,
            "--processes", str(processes),
            "--threads", str(threads),
            "--worker-shutdown-timeout", str(worker_shutdown_timeout),

            # --watch /path/to/project [--watch-use-polling]
            *watch_args,

            # [--fork-function import.path.function]*
            *forks_args,

            # -v -v ...
            *verbosity_args,

            # django_dramatiq.tasks app1.tasks app2.tasks ...
            *tasks_modules,
        ]

        if queues:
            process_args.extend(["--queues", *queues])

        if pid_file:
            process_args.extend(["--pid-file", pid_file])

        if log_file:
            process_args.extend(["--log-file", log_file])

        if skip_logging:
            process_args.append("--skip-logging")

        self.stdout.write(' * Running dramatiq: "%s"\n\n' % " ".join(process_args))

        if sys.platform == "win32":
            command = [executable_path] + process_args[1:]
            sys.exit(subprocess.run(command))

        os.execvp(executable_path, process_args)

    def discover_tasks_modules(self):
        task_module_names = getattr(settings, "DRAMATIQ_AUTODISCOVER_MODULES", ("tasks",))
        ignored_modules = set(getattr(settings, "DRAMATIQ_IGNORED_MODULES", []))
        app_configs = []
        for conf in apps.get_app_configs():
            # Always find our own tasks, regardless of the configured module names.
            if conf.name == "django_dramatiq":
                app_configs.append((conf, "tasks"))
            else:
                for task_module in task_module_names:
                    if module_has_submodule(conf.module, task_module):
                        app_configs.append((conf, task_module))
        tasks_modules = ["django_dramatiq.setup"]

        def is_ignored_module(module_name):
            if not ignored_modules:
                return False

            if module_name in ignored_modules:
                return True

            name_parts = module_name.split(".")

            for c in range(1, len(name_parts)):
                part_name = ".".join(name_parts[:c]) + ".*"
                if part_name in ignored_modules:
                    return True

            return False

        for conf, task_module in app_configs:
            module = conf.name + "." + task_module
            if is_ignored_module(module):
                self.stdout.write(" * Ignored tasks module: %r" % module)
                continue

            imported_module = importlib.import_module(module)
            if not self._is_package(imported_module):
                self.stdout.write(" * Discovered tasks module: %r" % module)
                tasks_modules.append(module)
            else:
                submodules = self._get_submodules(imported_module)

                for submodule in submodules:
                    if is_ignored_module(submodule):
                        self.stdout.write(" * Ignored tasks module: %r" % submodule)
                    else:
                        self.stdout.write(" * Discovered tasks module: %r" % submodule)
                        tasks_modules.append(submodule)

        return tasks_modules

    def _is_package(self, module):
        return hasattr(module, "__path__")

    def _get_submodules(self, package):
        submodules = []

        package_path = package.__path__
        prefix = package.__name__ + "."

        for _, module_name, _ in pkgutil.walk_packages(package_path, prefix):
            submodules.append(module_name)

        return submodules

    def _resolve_executable(self, exec_name):
        bin_dir = os.path.dirname(sys.executable)
        if bin_dir:
            for d in [bin_dir, os.path.join(bin_dir, "Scripts")]:
                exec_path = os.path.join(d, exec_name)
                if os.path.isfile(exec_path):
                    return exec_path
        return exec_name

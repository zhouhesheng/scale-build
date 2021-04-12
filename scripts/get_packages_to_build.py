#!/usr/bin/python3
import collections
import copy
import json
import os
import subprocess
import yaml

from toposort import toposort


DEPENDS_SCRIPT_PATH = './scripts/parse_deps.pl'


def run(*args, **kwargs):
    if isinstance(args[0], list):
        args = tuple(args[0])
    kwargs.setdefault('stdout', subprocess.PIPE)
    kwargs.setdefault('stderr', subprocess.PIPE)
    check = kwargs.pop('check', True)
    proc = subprocess.Popen(args, stdout=kwargs['stdout'], stderr=kwargs['stderr'])
    stdout, stderr = proc.communicate()
    cp = subprocess.CompletedProcess(args, proc.returncode, stdout=stdout, stderr=stderr)
    if check:
        cp.check_returncode()
    return cp


def normalize_bin_packages_depends(depends_str):
    return list(filter(lambda k: k and '$' not in k, map(str.strip, depends_str.split(','))))


def normalize_build_depends(build_depends_str):
    deps = []
    for dep in filter(bool, map(str.strip, build_depends_str.split(','))):
        for subdep in filter(bool, map(str.strip, dep.split('|'))):
            index = subdep.find('(')
            if index != -1:
                subdep = subdep[:index].strip()
            deps.append(subdep)
    return deps


def get_install_deps(packages, deps, deps_list):
    for dep in filter(lambda p: p in packages, deps_list):
        deps.add(packages[dep]['source'])
        deps.update(get_install_deps(packages, deps, packages[dep]['install_deps'] | packages[dep]['build_deps']))
    return deps


def retrieve_package_deps(sources_path, manifest):
    packages = collections.defaultdict(lambda: {'explicit_deps': set(), 'build_deps': set(), 'install_deps': set()})
    for package in manifest['sources']:
        name = package['name']
        package_path = os.path.join(sources_path, name)
        if not os.path.exists(package_path):
            raise FileNotFoundError(f'{package_path!r} not found, did you forget to "make checkout" ?')

        if package.get('subdir'):
            package_path = os.path.join(package_path, package['subdir'])

        if name == 'kernel' or (package.get('predepscmd') and not package.get('deps_path')):
            # We cannot determine dependency of this package because it does not probably have a control file
            # in it's current state - the only example we have is grub right now. Let's improve this if there are
            # more examples
            packages[name].update({
                'source_package': name,
                'source': name,
            })
            continue
        elif package.get('deps_path'):
            package_path = os.path.join(package_path, package['deps_path'], 'control')
        else:
            package_path = os.path.join(package_path, 'debian/control')

        cp = run([DEPENDS_SCRIPT_PATH, package_path])
        info = json.loads(cp.stdout)

        for bin_package in info['binary_packages']:
            default_dependencies = {'kernel'} if package.get('kernel_module') else set()
            packages[bin_package['name']].update({
                'build_deps': set(
                    normalize_build_depends(info['source_package']['build_depends'])
                ) | default_dependencies,
                'install_deps': set(normalize_bin_packages_depends(bin_package['depends'] or '')),
                'source_package': info['source_package']['name'],
                'source': name,
                'explicit_deps': set(package.get('explicit_deps', set())),
            })
            if name == 'truenas':
                packages[bin_package['name']]['build_deps'] |= packages[bin_package['name']]['install_deps']

    return {
        i['source']: get_install_deps(packages, set(), i['build_deps']) | i['explicit_deps']
        for n, i in packages.items()
    }


def retrieve_package_update_information(sources_path, manifest):
    package_deps = retrieve_package_deps(sources_path, manifest)
    hash_dir_path = os.environ['HASH_DIR']
    packages_info = {}
    for pkg in manifest['sources']:
        packages_info[pkg['name']] = {
            'rebuild': True,
            'deps': package_deps[pkg['name']],
        }
        if pkg['name'] == 'truenas':
            continue

        pkg_path = os.path.join(sources_path, pkg['name'])
        source_hash = run(['git', '-C', pkg_path, 'rev-parse', '--verify', 'HEAD']).stdout.decode().strip()
        existing_hash = None
        existing_hash_path = os.path.join(hash_dir_path, f'{pkg["name"]}.hash')
        if os.path.exists(existing_hash_path):
            with open(existing_hash_path, 'r') as f:
                existing_hash = f.read().strip()
        if source_hash == existing_hash:
            packages_info[pkg['name']]['rebuild'] = run(
                ['git', '-C', pkg_path, 'diff-files', '--quiet', '--ignore-submodules'], check=False
            ).returncode != 0

    # Now what we want to do is make sure if a parent package is to be rebuilt, we rebuild child packages
    parent_mapping = collections.defaultdict(set)
    for pkg, deps in package_deps.items():
        for dep in deps:
            parent_mapping[dep].add(pkg)

    for pkg, info in packages_info.items():
        if info['rebuild']:
            for child in parent_mapping[pkg]:
                packages_info[child]['rebuild'] = True

    # If a package is to be rebuilt, it does not mean it's dependencies necessarily have to be built again
    to_be_rebuilt_packages = {}
    for pkg, info in packages_info.items():
        if not info['rebuild']:
            continue
        to_be_rebuilt_packages[pkg] = {p for p in info['deps'] if packages_info[p]['rebuild']}

    return to_be_rebuilt_packages


if __name__ == '__main__':
    sources_path = os.environ['SOURCES']
    with open(os.environ['MANIFEST'], 'r') as f:
        manifest = yaml.safe_load(f.read())

    sorted_ordering = [list(deps) for deps in toposort(retrieve_package_update_information(sources_path, manifest))]
    parallel_builds = 1 if os.environ.get('PKG_DEBUG') else int(os.environ.get('PARALLEL_BUILDS') or 4)
    current_order = copy.deepcopy(sorted_ordering)
    sorted_ordering = []
    for index, batch in enumerate(current_order):
        while batch:
            sorted_ordering.append(batch[:parallel_builds])
            batch = batch[parallel_builds:]

    sources_info = {p['name']: p for p in manifest['sources']}
    package_deps = []
    for index, entry in enumerate(sorted_ordering):
        package_deps.append([])
        for pkg in entry:
            package_deps[index].append(sources_info[pkg])

    with open(os.environ['PKG_BUILD_MANIFEST'], 'w') as f:
        f.write(yaml.dump(package_deps))

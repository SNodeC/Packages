import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import chain
from common import resolved_packages
from plan import prepare


class DeclarationTests(unittest.TestCase):
    def test_selectors_add_only_the_matching_distribution_release(self):
        declaration = dict(build_dependencies={'deb': ['base'], 'rpm': ['native'], 'openwrt': ['feed'],
                                               'rpm@rocky-9': ['compiler'], 'openwrt@openwrt-24.10': ['extra']})
        for target, expected in [
            (dict(family='rpm', distribution='rocky', release='9'), ['native', 'compiler']),
            (dict(family='rpm', distribution='rocky', release='10'), ['native']),
            (dict(family='deb', distribution='debian', release='trixie'), ['base']),
            (dict(family='ipk', distribution='openwrt', release='24.10'), ['feed', 'extra']),
            (dict(family='apk', distribution='openwrt', release='25.12'), ['feed']),
        ]:
            with self.subTest(target=target):
                self.assertEqual(resolved_packages(declaration, target), expected)
        return None

    def test_plan_loads_schema_two_at_ci_commit_and_freezes_exact_dependencies(self):
        units = [dict(package=name, repo='owner/' + name, commit='a' * 40, ci_commit='b' * 40,
                      version='3.1.0', revision=7, action='build', dependencies=dependencies)
                 for name, dependencies in [('root', []), ('app', [dict(package='root', version='3.1.0', revision=7)])]]
        declarations = [dict(schema=2, package=name, title=name, depends_on=requirements,
                             build=dict(deb='ci/package/build.sh'), build_dependencies=dict(deb=[], rpm=[], openwrt=[]),
                             targets=['target']) for name, requirements in
                        [('root', []), ('app', [dict(package='root', version='>=3.0,<4')])]]
        plan = dict(feed_branch='feed-staging', targets=[dict(id='target', units=units)])
        with patch('plan.fetch', side_effect=[json.dumps(item).encode() for item in declarations]) as fetch:
            matrix = prepare(plan, dict(target=dict(id='target')), ['owner/root', 'owner/app'])
        self.assertEqual([call.args[0] for call in fetch.call_args_list],
                         ['https://raw.githubusercontent.com/owner/' + name + '/' + 'b' * 40 + '/ci/packages.json'
                          for name in ('root', 'app')])
        self.assertEqual(matrix[0]['units'][1]['dependencies'], units[1]['dependencies'])
        return None

    def test_build_checks_out_source_and_ci_independently(self):
        root = Path('/tmp/codex/build/Packages/tests')
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                def git(*args):
                    result = subprocess.run(['git', '-C', 'fixture', *args], check=True, capture_output=True, text=True)
                    return result.stdout.strip()

                Path('fixture/ci').mkdir(parents=True)
                git('init', '-q')
                git('config', 'user.name', 'Fixture')
                git('config', 'user.email', 'fixture@example.invalid')
                git('config', 'commit.gpgsign', 'false')
                Path('fixture/payload').write_text('tag source')
                Path('fixture/ci/build.sh').write_text('exit 99\n')
                git('add', '.')
                git('commit', '-qm', 'source')
                source_commit = git('rev-parse', 'HEAD')
                Path('fixture/payload').write_text('new source must not build')
                Path('fixture/ci/build.sh').write_text('set -eu\nmkdir -p "$OUTPUT_DIR"\n'
                                                    'cat "$SOURCE_DIR/payload" > "$OUTPUT_DIR/result.deb"\n'
                                                    'printf "%s" "$DEPENDENCIES_JSON" >> "$OUTPUT_DIR/result.deb"\n')
                git('add', '.')
                git('commit', '-qm', 'CI correction and unrelated source change')
                ci_commit = git('rev-parse', 'HEAD')
                real_run = chain.run

                def local_run(*args, **kwargs):
                    arguments = [str(Path('fixture').resolve()) if str(arg).startswith('https://github.com/') else arg
                                 for arg in args]
                    result = real_run(*arguments, **kwargs)
                    return result

                def hook(container, *args):
                    local = [arg.replace('/work/', str(Path('work').resolve()) + '/') for arg in args]
                    result = real_run(*local)
                    return result

                dependencies = [dict(package='library', version='3.0.0', revision=2)]
                unit = dict(package='fixture', repo='owner/fixture', commit=source_commit, ci_commit=ci_commit,
                            version='1.0.0', revision=1, dependencies=dependencies,
                            declaration=dict(build=dict(deb='ci/build.sh')))
                Path('artifacts').mkdir()
                with patch('chain.run', side_effect=local_run), patch('chain.as_builder', side_effect=hook), \
                        patch('chain.install_native'):
                    files = chain.build('container', dict(family='deb', codename='trixie'), unit, Path('artifacts'))
                self.assertEqual(Path('artifacts/fixture/result.deb').read_text(), 'tag source' + json.dumps(dependencies))
                self.assertEqual(list(files), ['fixture/result.deb'])
                self.assertEqual(Path('work/ci/fixture/build.sh').read_text(), Path('fixture/ci/build.sh').read_text())
            finally:
                os.chdir(original)
        return None

    def test_openwrt_installs_declared_feeds_and_catalog_host_packages(self):
        root = Path('/tmp/codex/build/Packages/tests')
        root.mkdir(parents=True, exist_ok=True)
        original = Path.cwd()
        target = dict(family='ipk', distribution='openwrt', release='24.10', host_packages=['sdk-host-fixture'],
                      sdk=dict(url='fixture/sdk', sha256='digest', sha256sums='fixture/sums'),
                      units=[dict(action='build', declaration=dict(build_dependencies=dict(openwrt=['feed-a', 'feed-b'])))])
        with tempfile.TemporaryDirectory(dir=root) as temporary:
            os.chdir(temporary)
            try:
                Path('work').mkdir()
                with patch('chain.fetch', side_effect=[b'archive', b'digest *sdk']), \
                        patch('chain.sha256', return_value='digest'), patch('chain.setup_run') as execute:
                    chain.setup('container', target)
                feeds = next(call.args for call in execute.call_args_list if './scripts/feeds install' in str(call.args))
                self.assertEqual(feeds[-3:], ('sh', 'feed-a', 'feed-b'))
                self.assertNotIn('install -a', ' '.join(feeds))
                install = next(call.args for call in execute.call_args_list if call.args[0] == 'dependencies')
                self.assertIn('sdk-host-fixture', install)
                self.assertNotIn('feed-a', install)
            finally:
                os.chdir(original)
        return None

    def test_central_tools_and_catalog_have_no_project_policy(self):
        root = Path(__file__).resolve().parents[1]
        names = ('snodec', 'snode.c', 'mqttsuite', 'aisuite', 'codexui', 'core-ci', 'pcre2', 'gcc-toolset')
        for path in [*(root / 'tools').glob('*.py'), root / 'ci/targets.json']:
            text = path.read_text().lower()
            self.assertTrue(all(name not in text for name in names), str(path))
        self.assertLessEqual(sum(len(path.read_text().splitlines()) for path in (root / 'tools').glob('*.py')), 1200)
        return None

# ==================================================================================================
# Copyright 2011 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

import os
import shutil
import sys

from contextlib import contextmanager
import itertools

from twitter.common.collections.orderedset import OrderedSet
from twitter.pants.base.worker_pool import Work
from twitter.pants.cache import create_artifact_cache

from twitter.pants.base.hash_utils import hash_file
from twitter.pants.base.build_invalidator import CacheKeyGenerator
from twitter.pants.binary_util import runjava_indivisible
from twitter.pants.goal.workunit import WorkUnit
from twitter.pants.reporting.reporting_utils import items_to_report_element
from twitter.pants.tasks.bootstrap_utils import BootstrapUtils
from twitter.pants.tasks.cache_manager import CacheManager, InvalidationCheck, VersionedTargetSet
from twitter.pants.tasks.ivy_utils import IvyUtils
from twitter.pants.tasks.task_error import TaskError



class Task(object):
  @classmethod
  def setup_parser(cls, option_group, args, mkflag):
    """Set up the cmd-line parser.

    Subclasses can add flags to the pants command line using the given option group.
    Flag names should be created with mkflag([name]) to ensure flags are properly namespaced
    amongst other tasks.
    """

  def __init__(self, context):
    self.context = context
    self.dry_run = self.can_dry_run() and context.options.dry_run
    self._cache_key_generator = CacheKeyGenerator(context.config.getdefault('cache_key_gen_version', default=None))
    self._artifact_cache_spec = None
    self._artifact_cache = None
    self._build_invalidator_dir = os.path.join(context.config.get('tasks', 'build_invalidator'),
                                               self.product_type())
    self._bootstrap_utils = BootstrapUtils(self.context.products)

  def setup_artifact_cache(self, spec):
    """Subclasses can call this in their __init__() to set up artifact caching for that task type.

    The cache is created lazily, as needed.

    spec should be a list of urls/file path prefixes, which are used in that order.
    By default, no artifact caching is used.
    """
    self._artifact_cache_spec = spec

  def create_artifact_cache(self, spec):
    if len(spec) > 0:
      pants_workdir = self.context.config.getdefault('pants_workdir')
      my_name = self.__class__.__name__
      return create_artifact_cache(self.context.log, pants_workdir, spec, my_name,
                                   self.context.options.local_artifact_cache_readonly,
                                   self.context.options.remote_artifact_cache_readonly)
    else:
      return None

  def get_artifact_cache(self):
    if self._artifact_cache is None and self._artifact_cache_spec is not None:
      self._artifact_cache = self.create_artifact_cache(self._artifact_cache_spec)
    return self._artifact_cache

  def product_type(self):
    """Set the product type for this task.

    By default, each task is considered as creating a unique product type.
    Subclasses can override this to specify a shared product type, e.g., 'classes'.

    Tasks with the same product type can invalidate each other's targets, e.g., if a ScalaLibrary
    depends on a JavaLibrary, a change to the JavaLibrary will invalidate the ScalaLibrary because
    they both have the same product type.
    """
    return self.__class__.__name__

  def can_dry_run(self):
    """Subclasses can override this to indicate that they respect the --dry-run flag.

    It's the subclass task's responsibility to do the right thing if this flag is set.

    Note that tasks such as codegen and ivy resolution cannot dry-run, because subsequent
    cache key computation will fail on missing sources/external deps.
    """
    return False

  def execute(self, targets):
    """Executes this task against targets, which may be a subset of the current context targets."""
    raise TaskError('execute() not implemented')

  def invalidate_for(self):
    """Provides extra objects that participate in invalidation.

    Subclasses can override and return an object that should be checked for changes when
    managing target invalidation.  If the pickled form of returned object changes
    between runs all targets will be invalidated.
    """
    return None

  def invalidate_for_files(self):
    """Provides extra files that participate in invalidation.

    Subclasses can override and return a list of full paths to extra, non-source files that should
    be checked for changes when managing target invalidation. This is useful for tracking
    changes to pre-built build tools, e.g., the thrift compiler.
    """
    return []

  @contextmanager
  def invalidated(self, targets, only_buildfiles=False, invalidate_dependents=False,
                  partition_size_hint=sys.maxint, silent=False):
    """Checks targets for invalidation, first checking the artifact cache.
    Subclasses call this to figure out what to work on.

    targets:               The targets to check for changes.
    only_buildfiles:       If True, then only the target's BUILD files are checked for changes, not
                           its sources.
    invalidate_dependents: If True then any targets depending on changed targets are invalidated.
    partition_size_hint:   Each VersionedTargetSet in the yielded list will represent targets
                           containing roughly this number of source files, if possible. Set to
                           sys.maxint for a single VersionedTargetSet. Set to 0 for one
                           VersionedTargetSet per target. It is up to the caller to do the right
                           thing with whatever partitioning it asks for.

    Yields an InvalidationCheck object reflecting the (partitioned) targets.

    If no exceptions are thrown by work in the block, the build cache is updated for the targets.
    Note: the artifact cache is not updated. That must be done manually.
    """
    extra_data = []
    extra_data.append(self.invalidate_for())

    for f in self.invalidate_for_files():
      extra_data.append(hash_file(f))

    cache_manager = CacheManager(self._cache_key_generator,
                                 self._build_invalidator_dir,
                                 invalidate_dependents,
                                 extra_data,
                                 only_externaldeps=only_buildfiles)

    invalidation_check = cache_manager.check(targets, partition_size_hint)

    if invalidation_check.invalid_vts and self.get_artifact_cache() and \
        self.context.options.read_from_artifact_cache:
      with self.context.new_workunit('cache'):
        cached_vts, uncached_vts = \
          self.check_artifact_cache(self.check_artifact_cache_for(invalidation_check))
      if cached_vts:
        cached_targets = [vt.target for vt in cached_vts]
        for t in cached_targets:
          self.context.run_tracker.artifact_cache_stats.add_hit('default', t)
        if not silent:
          self._report_targets('Using cached artifacts for ', cached_targets, '.')
      if uncached_vts:
        uncached_targets = [vt.target for vt in uncached_vts]
        for t in uncached_targets:
          self.context.run_tracker.artifact_cache_stats.add_miss('default', t)
        if not silent:
          self._report_targets('No cached artifacts for ', uncached_targets, '.')
      # Now that we've checked the cache, re-partition whatever is still invalid.
      invalidation_check = \
        InvalidationCheck(invalidation_check.all_vts, uncached_vts, partition_size_hint)

    if not silent:
      targets = []
      sources = []
      num_invalid_partitions = len(invalidation_check.invalid_vts_partitioned)
      for vt in invalidation_check.invalid_vts_partitioned:
        targets.extend(vt.targets)
        sources.extend(vt.cache_key.sources)
      if len(targets):
        msg_elements = ['Invalidated ',
                        items_to_report_element([t.address.reference() for t in targets], 'target')]
        if len(sources) > 0:
          msg_elements.append(' containing ')
          msg_elements.append(items_to_report_element(sources, 'source file'))
        if num_invalid_partitions > 1:
          msg_elements.append(' in %d target partitions' % num_invalid_partitions)
        msg_elements.append('.')
        self.context.log.info(*msg_elements)

    # Yield the result, and then mark the targets as up to date.
    yield invalidation_check
    if not self.dry_run:
      for vt in invalidation_check.invalid_vts:
        vt.update()  # In case the caller doesn't update.

  def check_artifact_cache_for(self, invalidation_check):
    """Decides which VTS to check the artifact cache for.

    By default we check for each invalid target. Can be overridden, e.g., to
    instead check only for a single artifact for the entire target set.
    """
    return invalidation_check.invalid_vts

  def check_artifact_cache(self, vts):
    """Checks the artifact cache for the specified list of VersionedTargetSets.

    Returns a pair (cached, uncached) of VersionedTargets that were
    satisfied/unsatisfied from the cache.
    """
    return self.do_check_artifact_cache(vts)

  def do_check_artifact_cache(self, vts, post_process_cached_vts=None):
    """Checks the artifact cache for the specified list of VersionedTargetSets.

    Returns a pair (cached, uncached) of VersionedTargets that were
    satisfied/unsatisfied from the cache.
    """
    if not vts:
      return [], []

    cached_vts = []
    uncached_vts = OrderedSet(vts)

    with self.context.new_workunit(name='check', labels=[WorkUnit.MULTITOOL]) as parent:
      res = self.context.submit_foreground_work_and_wait(
        Work(lambda vt: bool(self.get_artifact_cache().use_cached_files(vt.cache_key)),
             [(vt, ) for vt in vts], 'check'), workunit_parent=parent)
    for vt, was_in_cache in zip(vts, res):
      if was_in_cache:
        cached_vts.append(vt)
        uncached_vts.discard(vt)
    # Note that while the input vts may represent multiple targets (for tasks that overrride
    # check_artifact_cache_for), the ones we return must represent single targets.
    def flatten(vts):
      return list(itertools.chain.from_iterable([vt.versioned_targets for vt in vts]))
    all_cached_vts, all_uncached_vts = flatten(cached_vts), flatten(uncached_vts)
    if post_process_cached_vts:
      post_process_cached_vts(all_cached_vts)
    for vt in all_cached_vts:
      vt.update()
    return all_cached_vts, all_uncached_vts

  def update_artifact_cache(self, vts_artifactfiles_pairs):
    """Write to the artifact cache, if we're configured to.

    vts_artifactfiles_pairs - a list of pairs (vts, artifactfiles) where
      - vts is single VersionedTargetSet.
      - artifactfiles is a list of paths to artifacts for the VersionedTargetSet.
    """
    update_artifact_cache_work = self.get_update_artifact_cache_work(vts_artifactfiles_pairs)
    if update_artifact_cache_work:
      self.context.submit_background_work_chain([update_artifact_cache_work],
                                                parent_workunit_name='cache')

  def get_update_artifact_cache_work(self, vts_artifactfiles_pairs, cache=None):
    """Create a Work instance to update the artifact cache, if we're configured to.

    vts_artifactfiles_pairs - a list of pairs (vts, artifactfiles) where
      - vts is single VersionedTargetSet.
      - artifactfiles is a list of paths to artifacts for the VersionedTargetSet.
    """
    cache = cache or self.get_artifact_cache()
    if cache and self.context.options.write_to_artifact_cache and not cache.read_only:
      if len(vts_artifactfiles_pairs) == 0:
        return None
        # Do some reporting.
      targets = set()
      for vts, _ in vts_artifactfiles_pairs:
        targets.update(vts.targets)
      self._report_targets('Caching artifacts for ', list(targets), '.')
      # Cache the artifacts.
      args_tuples = []
      for vts, artifactfiles in vts_artifactfiles_pairs:
        if self.context.options.verify_artifact_cache:
          pass  # TODO: Verify that the artifact we just built is identical to the cached one?
        args_tuples.append((vts.cache_key, artifactfiles))
      return Work(lambda *args: cache.insert(*args), args_tuples, 'insert')
    else:
      return None

  def _report_targets(self, prefix, targets, suffix):
    self.context.log.info(
      prefix,
      items_to_report_element([t.address.reference() for t in targets], 'target'),
      suffix)

  def ivy_resolve(self, targets, java_runner=None, ivy_args=None, symlink_ivyxml=False, silent=False,
                  workunit_name=None, workunit_labels=None):
    java_runner = java_runner or runjava_indivisible

    ivy_args = ivy_args or []

    targets = set(targets)

    if not targets:
      return []
    
    work_dir = self.context.config.get('ivy-resolve', 'workdir')
    confs = self.context.config.getlist('ivy-resolve', 'confs')

    with self.invalidated(targets,
                          only_buildfiles=True,
                          invalidate_dependents=True,
                          silent=silent) as invalidation_check:
      global_vts = VersionedTargetSet.from_versioned_targets(invalidation_check.all_vts)
      target_workdir = os.path.join(work_dir, global_vts.cache_key.hash)
      target_classpath_file = os.path.join(target_workdir, 'classpath')
      target_classpath_file_tmp = target_classpath_file + '.tmp'
      # Note that it's possible for all targets to be valid but for no classpath file to exist at
      # target_classpath_file, e.g., if we previously built a superset of targets.
      if invalidation_check.invalid_vts or not os.path.exists(target_classpath_file):
        ivy_utils = IvyUtils(config=self.context.config,
                             options=self.context.options,
                             log=self.context.log)
        args = (['-cachepath', target_classpath_file_tmp] +
                ['-confs'] + confs +
                ivy_args)

        def exec_ivy():
          ivy_utils.exec_ivy(
            target_workdir=target_workdir,
            targets=targets,
            args=args,
            runjava=java_runner,
            workunit_name='ivy',
            workunit_factory=self.context.new_workunit,
            symlink_ivyxml=symlink_ivyxml,
          )

        if workunit_name:
          with self.context.new_workunit(name=workunit_name, labels=workunit_labels or []):
            exec_ivy()
        else:
          exec_ivy()

        if not os.path.exists(target_classpath_file_tmp):
          raise TaskError('Ivy failed to create classpath file at %s' % target_classpath_file_tmp)
        shutil.move(target_classpath_file_tmp, target_classpath_file)
        if self.get_artifact_cache() and self.context.options.write_to_artifact_cache:
          self.update_artifact_cache([(global_vts, [target_classpath_file])])

    with IvyUtils.cachepath(target_classpath_file) as classpath:
      stripped_classpath = [path.strip() for path in classpath]
      return [path for path in stripped_classpath if IvyUtils.is_mappable_artifact(path)]

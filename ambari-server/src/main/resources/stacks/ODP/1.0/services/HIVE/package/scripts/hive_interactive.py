#!/usr/bin/env python
"""
Licensed to the Apache Software Foundation (ASF) under one
or more contributor license agreements.  See the NOTICE file
distributed with this work for additional information
regarding copyright ownership.  The ASF licenses this file
to you under the Apache License, Version 2.0 (the
"License"); you may not use this file except in compliance
with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

"""

# Python Imports
import os
from urlparse import urlparse

# Local Imports
from hive import fill_conf_dir, jdbc_connector

# Ambari Commons & Resource Management Imports
from ambari_commons.constants import SERVICE

from resource_management.core.logger import Logger
from resource_management.core.source import Template, DownloadSource, InlineTemplate
from resource_management.core.resources.system import File, Directory
from resource_management.libraries.functions.default import default
from resource_management.libraries.functions.format import format
from resource_management.libraries.functions.generate_logfeeder_input_config import generate_logfeeder_input_config
from resource_management.libraries.functions.is_empty import is_empty
from resource_management.libraries.functions.security_commons import update_credential_provider_path
from resource_management.libraries.functions.setup_atlas_hook import setup_atlas_hook
from resource_management.libraries.resources.hdfs_resource import HdfsResource
from resource_management.libraries.resources.xml_config import XmlConfig


"""
Sets up the configs, jdbc connection and tarball copy to HDFS for Hive Server Interactive.
"""
def hive_interactive(name = None):
  import params
  
  MB_TO_BYTES = 1048576

  # create tez folder for llap
  Logger.info("Create tez folder for LLAP")
  Directory(params.tez_interactive_conf_dir,
        create_parents = True,
        cd_access="a",
        mode=0775,
        owner=params.tez_user,
        group=params.user_group,
        ignore_failures=True,
  )

  # if warehouse directory is in DFS
  if not params.whs_dir_protocol or params.whs_dir_protocol == urlparse(params.default_fs).scheme:
    # Create Hive Metastore Warehouse Dir
    params.HdfsResource(params.hive_metastore_warehouse_dir,
                        type = "directory",
                        action = "create_on_execute",
                        owner = params.hive_user,
                        group = params.user_group,
                        mode = 0700
    )
    # create directories for llap package
    pkg_dir = '/user/' + params.hive_user + '/.yarn'
    for dir in [pkg_dir, pkg_dir + '/package', pkg_dir + '/package/LLAP']:
      # hdfsresouces handles parent creation badly
      params.HdfsResource(dir,
                          type = "directory",
                          action = "create_on_execute",
                          owner = params.hive_user,
                          group = params.user_group,
                          mode = 0755
      )

    if not is_empty(params.tez_hook_proto_base_directory):
      params.HdfsResource(params.tez_hook_proto_base_directory,
                          type = "directory",
                          action = "create_on_execute",
                          owner = params.hive_user,
                          mode = 01755
                          )

    if not is_empty(params.hive_hook_proto_base_directory):
        params.HdfsResource(params.hive_hook_proto_base_directory,
                            type = "directory",
                            action = "create_on_execute",
                            owner = params.hive_user,
                            mode = 01777
                            )

        dag_meta = params.tez_hook_proto_base_directory + "dag_meta"
        params.HdfsResource(dag_meta,
                            type = "directory",
                            action = "create_on_execute",
                            owner = params.hive_user,
                            mode = 01777
                            )

        dag_data = params.tez_hook_proto_base_directory + "dag_data"
        params.HdfsResource(dag_data,
                            type = "directory",
                            action = "create_on_execute",
                            owner = params.hive_user,
                            mode = 01777
                            )

        app_data = params.tez_hook_proto_base_directory + "app_data"
        params.HdfsResource(app_data,
                            type="directory",
                            action="create_on_execute",
                            owner=params.hive_user,
                            mode=01777
                            )

  else:
    Logger.info(format("Not creating warehouse directory '{hive_metastore_warehouse_dir}', as the location is not in DFS."))


  # Create Hive User Dir
  params.HdfsResource(params.hive_hdfs_user_dir,
                      type = "directory",
                      action = "create_on_execute",
                      owner = params.hive_user,
                      mode = params.hive_hdfs_user_mode
  )

  params.HdfsResource(None, action="execute")

  # list of properties that should be excluded from the config
  # this approach is a compromise against adding a dedicated config
  # type for hive_server_interactive or needed config groups on a
  # per component basis
  exclude_list = ['hive.enforce.bucketing',
                  'hive.enforce.sorting']

  # List of configs to be excluded from hive2 client, but present in Hive2 server.
  exclude_list_for_hive2_client = ['javax.jdo.option.ConnectionPassword',
                                   'hadoop.security.credential.provider.path']

  Logger.info("Directories to fill with configs: %s" % str(params.hive_conf_dirs_list))
  for conf_dir in params.hive_conf_dirs_list:
    fill_conf_dir(conf_dir)

  '''
  As hive2/hive-site.xml only contains the new + the changed props compared to hive/hive-site.xml,
  we need to merge hive/hive-site.xml and hive2/hive-site.xml and store it in hive2/hive-site.xml.
  '''
  merged_hive_interactive_site = {}
  merged_hive_interactive_site.update(params.hive_site_config)
  merged_hive_interactive_site.update(params.config['configurations']['hive-interactive-site'])
  for item in exclude_list:
    if item in merged_hive_interactive_site.keys():
      del merged_hive_interactive_site[item]

  '''
  Config 'hive.llap.io.memory.size' calculated value in stack_advisor is in MB as of now. We need to
  convert it to bytes before we write it down to config file.
  '''
  if 'hive.llap.io.memory.size' in merged_hive_interactive_site.keys():
    hive_llap_io_mem_size_in_mb = merged_hive_interactive_site.get("hive.llap.io.memory.size")
    hive_llap_io_mem_size_in_bytes = long(hive_llap_io_mem_size_in_mb) * MB_TO_BYTES
    merged_hive_interactive_site['hive.llap.io.memory.size'] = hive_llap_io_mem_size_in_bytes
    Logger.info("Converted 'hive.llap.io.memory.size' value from '{0} MB' to '{1} Bytes' before writing "
                "it to config file.".format(hive_llap_io_mem_size_in_mb, hive_llap_io_mem_size_in_bytes))

  '''
  Hive2 doesn't have support for Atlas, we need to remove the Hook 'org.apache.atlas.hive.hook.HiveHook',
  which would have come in config 'hive.exec.post.hooks' during the site merge logic, if Atlas is installed.
  '''
  # Generate atlas-application.properties.xml file
  if params.enable_atlas_hook and params.stack_supports_atlas_hook_for_hive_interactive:
    Logger.info("Setup for Atlas Hive2 Hook started.")

    atlas_hook_filepath = os.path.join(params.hive_server_interactive_conf_dir, params.atlas_hook_filename)
    setup_atlas_hook(SERVICE.HIVE, params.hive_atlas_application_properties, atlas_hook_filepath, params.hive_user, params.user_group)

    Logger.info("Setup for Atlas Hive2 Hook done.")
  else:
    # Required for HDP 2.5 stacks
    Logger.info("Skipping setup for Atlas Hook, as it is disabled/ not supported.")
    remove_atlas_hook_if_exists(merged_hive_interactive_site)

  '''
  As tez_hive2/tez-site.xml only contains the new + the changed props compared to tez/tez-site.xml,
  we need to merge tez/tez-site.xml and tez_hive2/tez-site.xml and store it in tez_hive2/tez-site.xml.
  '''
  merged_tez_interactive_site = {}
  if 'tez-site' in params.config['configurations']:
    merged_tez_interactive_site.update(params.config['configurations']['tez-site'])
    Logger.info("Retrieved 'tez/tez-site' for merging with 'tez_hive2/tez-interactive-site'.")
  else:
    Logger.error("Tez's 'tez-site' couldn't be retrieved from passed-in configurations.")

  merged_tez_interactive_site.update(params.config['configurations']['tez-interactive-site'])
  
  XmlConfig("tez-site.xml",
            conf_dir = params.tez_interactive_conf_dir,
            configurations = merged_tez_interactive_site,
            configuration_attributes=params.config['configurationAttributes']['tez-interactive-site'],
            owner = params.tez_interactive_user,
            group = params.user_group,
            mode = 0664)

  '''
  Merge properties from hiveserver2-interactive-site into hiveserver2-site
  '''
  merged_hiveserver2_interactive_site = {}
  if 'hiveserver2-site' in params.config['configurations']:
    merged_hiveserver2_interactive_site.update(params.config['configurations']['hiveserver2-site'])
    Logger.info("Retrieved 'hiveserver2-site' for merging with 'hiveserver2-interactive-site'.")
  else:
    Logger.error("'hiveserver2-site' couldn't be retrieved from passed-in configurations.")
  merged_hiveserver2_interactive_site.update(params.config['configurations']['hiveserver2-interactive-site'])


  # Create config files under hive_server_interactive_conf_dir:
  #   hive-site.xml
  #   hive-env.sh
  #   llap-daemon-log4j2.properties
  #   llap-cli-log4j2.properties
  #   hive-log4j2.properties
  #   hive-exec-log4j2.properties
  #   beeline-log4j2.properties

  hive_server_interactive_conf_dir = params.hive_server_interactive_conf_dir

  mode_identified = 0600
  merged_hive_interactive_site = update_credential_provider_path(merged_hive_interactive_site,
                                                                 'hive-site',
                                                                 os.path.join(conf_dir, 'hive-site.jceks'),
                                                                 params.hive_user,
                                                                 params.user_group
  )
  XmlConfig("hive-site.xml",
            conf_dir = hive_server_interactive_conf_dir,
            configurations = merged_hive_interactive_site,
            configuration_attributes = params.config['configurationAttributes']['hive-interactive-site'],
            owner = params.hive_user,
            group = params.user_group,
            mode = 0644
  )
  XmlConfig("hiveserver2-site.xml",
            conf_dir = hive_server_interactive_conf_dir,
            configurations = merged_hiveserver2_interactive_site,
            configuration_attributes = params.config['configurationAttributes']['hiveserver2-interactive-site'],
            owner = params.hive_user,
            group = params.user_group,
            mode = mode_identified
  )

  File(format("{hive_server_interactive_conf_dir}/hive-env.sh"),
       owner = params.hive_user,
       group = params.user_group,
       mode = 0755,
       content = InlineTemplate(params.hive_interactive_env_sh_template)
  )

  llap_daemon_log4j_filename = 'llap-daemon-log4j2.properties'
  File(format("{hive_server_interactive_conf_dir}/{llap_daemon_log4j_filename}"),
       mode = mode_identified,
       group = params.user_group,
       owner = params.hive_user,
       content = InlineTemplate(params.llap_daemon_log4j)
  )

  llap_cli_log4j2_filename = 'llap-cli-log4j2.properties'
  File(format("{hive_server_interactive_conf_dir}/{llap_cli_log4j2_filename}"),
       mode = mode_identified,
       group = params.user_group,
       owner = params.hive_user,
       content = InlineTemplate(params.llap_cli_log4j2)
  )

  hive_log4j2_filename = 'hive-log4j2.properties'
  File(format("{hive_server_interactive_conf_dir}/{hive_log4j2_filename}"),
       mode = mode_identified,
       group = params.user_group,
       owner = params.hive_user,
       content = InlineTemplate(params.hive_log4j2)
  )

  hive_exec_log4j2_filename = 'hive-exec-log4j2.properties'
  File(format("{hive_server_interactive_conf_dir}/{hive_exec_log4j2_filename}"),
       mode = mode_identified,
       group = params.user_group,
       owner = params.hive_user,
       content = InlineTemplate(params.hive_exec_log4j2)
  )

  beeline_log4j2_filename = 'beeline-log4j2.properties'
  File(format("{hive_server_interactive_conf_dir}/{beeline_log4j2_filename}"),
       mode = mode_identified,
       group = params.user_group,
       owner = params.hive_user,
       content = InlineTemplate(params.beeline_log4j2)
  )

  XmlConfig("beeline-site.xml",
            conf_dir=conf_dir,
            configurations=params.beeline_site_config,
            owner=params.hive_user,
            group=params.user_group,
            mode=mode_identified)

  File(os.path.join(hive_server_interactive_conf_dir, "hadoop-metrics2-hiveserver2.properties"),
       owner = params.hive_user,
       group = params.user_group,
       mode = mode_identified,
       content = Template("hadoop-metrics2-hiveserver2.properties.j2")
  )

  File(format("{hive_server_interactive_conf_dir}/hadoop-metrics2-llapdaemon.properties"),
       owner = params.hive_user,
       group = params.user_group,
       mode = mode_identified,
       content = Template("hadoop-metrics2-llapdaemon.j2")
  )

  File(format("{hive_server_interactive_conf_dir}/hadoop-metrics2-llaptaskscheduler.properties"),
       owner = params.hive_user,
       group = params.user_group,
       mode = mode_identified,
       content = Template("hadoop-metrics2-llaptaskscheduler.j2")
  )

  # On some OS this folder could be not exists, so we will create it before pushing there files
  Directory(params.limits_conf_dir,
            create_parents = True,
            owner = 'root',
            group = 'root'
  )

  File(os.path.join(params.limits_conf_dir, 'hive.conf'),
       owner = 'root',
       group = 'root',
       mode = 0644,
       content=Template("hive.conf.j2")
  )

  if not os.path.exists(params.target_hive_interactive):
    jdbc_connector(params.target_hive_interactive, params.hive_intaractive_previous_jdbc_jar)

  File(format("/usr/lib/ambari-agent/{check_db_connection_jar_name}"),
       content = DownloadSource(format("{jdk_location}/{check_db_connection_jar_name}")),
       mode = 0644
  )
  File(params.start_hiveserver2_interactive_path,
       mode = 0755,
       content = Template(format('{start_hiveserver2_interactive_script}'))
  )

  Directory(params.hive_pid_dir,
            create_parents = True,
            cd_access = 'a',
            owner = params.hive_user,
            group = params.user_group,
            mode = 0755
  )
  Directory(params.hive_log_dir,
            create_parents = True,
            cd_access = 'a',
            owner = params.hive_user,
            group = params.user_group,
            mode = 0755
  )
  Directory(params.hive_interactive_var_lib,
            create_parents = True,
            cd_access = 'a',
            owner = params.hive_user,
            group = params.user_group,
            mode = 0755
  )
  generate_logfeeder_input_config('hive', Template("input.config-hive.json.j2", extra_imports=[default]))

"""
Remove 'org.apache.atlas.hive.hook.HiveHook' value from Hive2/hive-site.xml config 'hive.exec.post.hooks', if exists.
"""
def remove_atlas_hook_if_exists(merged_hive_interactive_site):
  if 'hive.exec.post.hooks' in merged_hive_interactive_site.keys():
    existing_hive_exec_post_hooks = merged_hive_interactive_site.get('hive.exec.post.hooks')
    if existing_hive_exec_post_hooks:
      hook_splits = existing_hive_exec_post_hooks.split(",")
      updated_hook_splits = [hook for hook in hook_splits if not hook.strip() == 'org.apache.atlas.hive.hook.HiveHook']
      updated_hooks_str = ",".join((str(hook)).strip() for hook in updated_hook_splits)
      if updated_hooks_str != existing_hive_exec_post_hooks:
        merged_hive_interactive_site['hive.exec.post.hooks'] = updated_hooks_str
        Logger.info("Updated Hive2/hive-site.xml 'hive.exec.post.hooks' value from : '{0}' to : '{1}'"
                    .format(existing_hive_exec_post_hooks, updated_hooks_str))
      else:
        Logger.info("No change done to Hive2/hive-site.xml 'hive.exec.post.hooks' value.")
  else:
    Logger.debug("'hive.exec.post.hooks' doesn't exist in Hive2/hive-site.xml")

#!/usr/bin/env python

import ConfigParser
import StringIO
from base64 import b64encode

import os
import json
import math

from vc3master.task import VC3Task
from vc3infoservice.infoclient import InfoConnectionFailure

import pluginmanager as pm
import traceback

class HandleRequests(VC3Task):
    '''
    Plugin to manage the life cycle of all requests.
     
    '''

    def __init__(self, parent, config, section):
        super(HandleRequests, self).__init__(parent, config, section)
        self.client = parent.client
        self.log.debug("HandleRequests VC3Task initialized.")

    def runtask(self):
        self.log.info("Running task %s" % self.section)
        self.log.debug("Polling master....")

        try:
            requests = self.client.listRequests()
            n = len(requests) if requests else 0
            self.log.debug("Processing %d requests" % n)
            if requests:
                for r in requests:
                    try:
                        self.process_request(r)
                    except VC3InvalidRequest, e:
                        self.log.warning("Request %s is not valid. (%s)", request.name, e)
                    except Exception, e:
                        self.log.warning("Request %s had a exception (%s)", request.name, e)
                        self.log.debug(traceback.format_exc(None))
        except InfoConnectionFailure, e:
            self.log.warning("Could not read requests from infoservice. (%s)", e)

    def process_request(self, request):
        next_state  = None
        reason      = None

        self.log.debug("Processing request '%s'", request.name)

        (next_state, reason) = (request.state, request.state_reason)

        if request.action and request.action == 'terminate':
            if not self.is_finishing_state(next_state):
                (next_state, reason) = ('terminating', 'received terminate action')

        nodesets           = self.getNodesets(request)
        request.statusinfo = self.compute_job_status_summary(request.statusraw, nodesets, next_state)

        if  next_state == 'new': 
            # nexts: validated, terminating
            (next_state, reason) = self.state_new(request)

        if  next_state == 'validated':
            # nexts: validated, pending, terminating
            (next_state, reason) = self.state_validated(request)

        if next_state == 'pending':
            # nexts: pending, running, terminating
            (next_state, reason) = self.state_pending(request)

        if next_state == 'running':
            # nexts: running, terminating
            (next_state, reason) = self.state_running(request)

        if next_state == 'terminating':
            # waits until everything has been cleanup
            (next_state, reason) = self.state_terminating(request)

        if next_state == 'cleanup':
            # waits until everything has been cleanup
            (next_state, reason) = self.state_cleanup(request)

        if next_state == 'terminated':
            self.log.debug('request %s is done' % request.name)
            (next_state, reason) = ('terminated', None)

        if next_state is not request.state or reason is not request.state_reason:
            self.log.debug("request '%s'  state '%s' -> %s (%s)'", request.name, request.state, next_state, str(reason))
            request.state = next_state
            request.state_reason = reason
        else:
            self.log.debug("request '%s' remained in state '%s'", request.name, request.state)

        try:
            self.add_queues_conf(request, nodesets)
            self.add_auth_conf(request)
            self.client.storeRequest(request)
        except Exception, e:
            self.log.warning("Storing the new request state failed. (%s)", e)

    def is_finishing_state(self, state):
        return state in ['terminating', 'cleanup', 'terminated']

    def request_is_valid(self, request):
        return True

    def state_new(self, request):
        '''
        Validates all new requests. 
        '''
        self.log.debug('processing new request %s' % request.name)
        if self.request_is_valid(request):
            return ('validated', None)
        self.log.warning("Invalid Request: %s" % str(e))
        return ('terminated', 'Invalid request: %s' % e.reason)

    def state_validated(self, request):
        self.log.debug('waiting for factory to configure %s' % request.name)

        running = self.job_count_with_state(request, 'running')
        idle    = self.job_count_with_state(request, 'idle')

        if (running is None) or (idle is None) or (idle + running == 0):
            # factory has not reported any jobs, so we remain in the validated state. 
            return ('validated', None)
        else:
            # factory updated the status of the queue, so we know the request is configured.
            return ('pending', 'Waiting for factory to start filling the request.')

    def state_pending(self, request):
        self.log.debug('waiting for factory to start fulfilling request %s' % request.name)

        running = self.job_count_with_state(request, 'running')

        if running is None:
            self.log.warning('Failure: status of request %s went away.' % request.name)
            return ('terminating', 'Failure: status of request %s went away.' % request.name)
        elif running > 0:
            return ('running', 'factory started fulfilling request %s.' % request.name)
        else:
            return ('pending', 'Waiting for factory to start filling the request.')


    def state_running(self, request):
        total_of_nodes = self.total_jobs_requested(request)
        running        = self.job_count_with_state(request, 'running')

        if running is None:
            self.log.warning('Failure: status of request %s went away.' % request.name)
            return ('terminating', 'Failure: status of request %s went away.' % request.name)
        elif total_of_nodes > running:
            return ('running', 'growing' % (total_of_nodes - running))
        elif total_of_nodes < running:
            return ('running', 'shrinking' % (running - total_of_nodes))
        else:
            return ('running', 'all requested jobs are running.')

    def state_terminating(self, request):
        self.log.debug('request %s is terminating' % request.name)
        running = self.job_count_with_state(request, 'running')
        idle    = self.job_count_with_state(request, 'idle')

        if (running is None) or (idle is None):
            self.log.warning('Failure: status of request %s went away.' % request.name)
            return ('cleanup', 'Failure: status of request %s went away.' % request.name)
        elif (running + idle) == 0:
            return ('cleanup', 'Factory finished draining the request.')
        else:
            return ('terminating', None)


    def state_cleanup(self, request):
        self.log.debug('collecting garbage for request %s' % request.name)

        # to fill cleanup here!
        if self.is_everything_cleaned_up(request):
            return ('terminated', 'Garbage collected')


    def add_queues_conf(self, request, nodesets):
        '''
            request.allocations = [ alloc1, alloc2 ]
                   .cluster.nodesets = [ nodeset1, nodeset2 ]                                       
             nodeset.node_number   # total number to launch. 
        '''
        config = ConfigParser.RawConfigParser()

        for allocation_name in request.allocations:
            self.generate_queues_section(config, request, nodesets, allocation_name)

        conf_as_string = StringIO.StringIO()
        config.write(conf_as_string)

        request.queuesconf = b64encode(conf_as_string.getvalue())
        return request.queuesconf

    def add_auth_conf(self, request):
        config = ConfigParser.RawConfigParser()

        for allocation_name in request.allocations:
            self.generate_auth_section(config, request, allocation_name)

        conf_as_string = StringIO.StringIO()
        config.write(conf_as_string)

        request.authconf = b64encode(conf_as_string.getvalue())
        return request.authconf

    def generate_queues_section(self, config, request, nodesets, allocation_name):
        allocation = self.client.getAllocation(allocation_name)
        if not allocation:
            raise VC3InvalidRequest("Allocation '%s' has not been declared." % allocation_name, request = request)

        resource = self.client.getResource(allocation.resource)
        if not resource:
            raise VC3InvalidRequest("Resource '%s' has not been declared." % allocation.resource, request = request)
        
        for nodeset in nodesets:
            self.add_nodeset_to_queuesconf(config, request, resource, allocation, nodeset)

    def add_nodeset_to_queuesconf(self, config, request, resource, allocation, nodeset):
        node_number  = self.jobs_to_run_by_policy(request, allocation, nodeset)
        section_name = request.name + '.' + nodeset.name + '.' + allocation.name

        self.log.debug("Information finalized for queues configuration section [%s]. Creating config." % section_name)

        config.add_section(section_name)
        config.set(section_name, 'sched.keepnrunning.keep_running', node_number)

        if resource.accesstype == 'batch':
            config.set(section_name, 'batchsubmitplugin',           'CondorSSH')
            config.set(section_name, 'batchsubmit.condorssh.user',  allocation.accountname)
            config.set(section_name, 'batchsubmit.condorssh.batch', resource.accessflavor)
            config.set(section_name, 'batchsubmit.condorssh.host',  resource.accesshost)
            config.set(section_name, 'batchsubmit.condorssh.port',  str(resource.accessport))
            config.set(section_name, 'batchsubmit.condorssh.authprofile', allocation.name)
            config.set(section_name, 'executable',          '       %(builder)s')
        elif resource.accesstype == 'cloud':
            config.set(section_name, 'batchsubmitplugin',          'CondorEC2')
        elif resource.accesstype == 'local':
            config.set(section_name, 'batchsubmitplugin',          'CondorLocal')
            config.set(section_name, 'batchsubmit.condorlocal.condor_attributes.should_transfer_files', 'YES')
            config.set(section_name, 'batchsubmit.condorlocal.condor_attributes.initialdir', '$ENV(TMP)')
            config.set(section_name, 'executable',                 '%(builder)s')
        else:
            raise VC3InvalidRequest("Unknown resource access type '%s'" % str(resource.accesstype), request = request)

        self.add_environment_to_queuesconf(config, request, section_name, nodeset)

        self.log.debug("Completed filling in config for allocation %s" % allocation.name)


    def jobs_to_run_by_policy(self, request, allocation, nodeset):
        self.log.debug("Calculating nodes to run...")
        node_number = 0

        if self.is_finishing_state(request.state):
            node_number = 0
            self.log.debug("Request in finishing state. Setting keepnrunning to 0")
        else:
            # For now no policies. Just calculated static-balanced 
            node_number = self.jobs_to_run_by_static_balanced(request, allocation, nodeset)
        return node_number
    
    def jobs_to_run_by_static_balanced(self, request, allocation, nodeset):
        numalloc     = len(request.allocations)
        total_to_run = request.statusinfo[nodeset.name]['requested']
        raw          = int(math.floor(float(total_to_run) / numalloc))
        total_raw    = raw * numalloc

        # since using floor, it is always the case that total_raw <= total_to_run
        # we compensate for any difference in the allocation last in the list
        node_number = raw
        diff        = total_to_run - total_raw

        if allocation.name == request.allocations[-1]:
            node_number += diff

        self.log.debug("With %d allocations and nodeset.node_number %d this allocation should run %d" % (numalloc, total_to_run, node_number))
        return node_number

    def generate_auth_tokens(self, principle):
        """ 
        Generate SSH priv/pub keys and base64 encode them
        """ 
        self.log.info("Generating or retrieving SSH keys for %s", principle)
        self.ssh = self.parent.parent.ssh
        (pub, priv) = self.ssh.getkeys(principle)
        self.log.debug("public key: %s", pub)
        encoded_pub = b64encode(pub)
        encoded_priv = b64encode(priv)
        return encoded_pub, encoded_priv
         

    def generate_auth_section(self, config, request, allocation_name):

        name = allocation_name
        config.add_section(name)

        allocation = self.client.getAllocation(allocation_name)
        if not allocation:
            raise VC3InvalidRequest("Allocation '%s' has not been declared." % allocation_name, request = request)

        if allocation.pubtoken is None:
            raise VC3InvalidRequest("Allocation '%s' doesn't have pub token." % allocation_name, request = request)

        if allocation.privtoken is None:
            raise VC3InvalidRequest("Allocation '%s' doesn't have pub token." % allocation_name, request = request)

        resource = self.client.getResource(allocation.resource)
        if not resource:
            raise VC3InvalidRequest("Resource '%s' has not been declared." % allocation.resource, request = request)

        if resource.accessmethod == 'ssh':
            # credible always generates rsa keys
            #allocation.sectype = 'ssh-rsa'
            #(allocation.pubtoken, allocation.privtoken) = self.generate_auth_tokens(name)
            
            config.set(name, 'plugin',        'SSH')
            config.set(name, 'ssh.type',  allocation.sectype)
            config.set(name, 'ssh.publickey', allocation.pubtoken)
            config.set(name, 'ssh.privatekey', allocation.privtoken)
        elif resource.accesstype == 'gsissh':
            raise NoImplementedError
        elif resource.accesstype == 'local':
            # nothing special is needed
            config.set(name, 'plugin',        'Noop')
        else:
            raise VC3InvalidRequest("Unknown resource access method '%s'" % str(resource.accessmethod), request = request)


    def add_pilot_to_queuesconf(self, config, request, section_name, nodeset):

        s = ''
        if nodeset.app_type == 'htcondor':
            collector = 'condor-dev.virtualclusters.org'
            s += ' --sys python:2.7=/usr'
            s += ' --require vc3-glidein'
            s += ' -- vc3-glidein -c %s -C %s -p mycondorpassword' % (collector, collector)
        elif nodeset.app_type == 'workqueue':
            s += ' --require cctools-statics'
            s += ' -- work_queue_worker -M %s -t 1800' % (request.name,)
        else:
            raise VC3InvalidRequest("Unknown nodeset app_type: '%s'" % nodeset.app_type)

        return s


    def add_environment_to_queuesconf(self, config, request, section_name, nodeset):
        #s  = " --revar 'VC3_.*'"
        s  = ' '
        s += ' --home=.'
        s += ' --install=.'

        if nodeset.environment is not None:
            environment = self.client.getEnvironment(nodeset.environment)
            if environment is None:
                raise VC3InvalidRequest("Unknown environment '%s' for '%s'" % (nodeset.environment, section_name), request = request)

            config.set(section_name, 'vc3.environment', environment.name)

            vs    = [ "VC3_REQUESTID=%s" % request.name, "VC3_QUEUE=%s" % section_name]
            for k in environment.envmap:
                vs.append("%s=%s" % (k, environment.envmap[k]))

            reqs  = ' '.join(['--require %s' % x for x in environment.packagelist])
            vars  = ' '.join(['--var %s' % x for x in vs])

            s += ' ' + vars + ' ' + reqs

            if environment.builder_extra_args:
                s += ' ' + ' '.join(environment.builder_extra_args)

            if environment.command:
                self.log.warning('Ignoring command of environment %s for %s. Adding pilot for %s instead' % (environment.name, section_name, nodeset.name))

        s += ' ' + self.add_pilot_to_queuesconf(config, request, section_name, nodeset)

        config.set(section_name, 'executable.arguments', s)

    def is_everything_cleaned_up(self, request):
        ''' TO BE FILLED '''
        return True

    def job_count_with_state(self, request, state):
        if not request.statusraw:
            return None

        at_least_one_nodeset = False
        count = 0

        for nodeset in request.statusinfo.keys():
            count += request.statusinfo[nodeset].get(state, 0)
            at_least_one_nodeset = True

        if at_least_one_nodeset:
            return count

        return None


    def total_jobs_requested(self, request):
        if self.is_finishing_state(request.state):
            return 0

        if not request.statusinfo:
            raise Exception("request.statusinfo not available. This should not ever happen...")

        cluster = self.client.getCluster(request.cluster)

        if len(cluster.nodesets) < 1:
            raise VC3InvalidRequest("No nodesets have been added to Cluster '%s' " % cluster.name, request = request)

        total_jobs = 0
        for nodeset_name in cluster.nodesets:
            nodeset = self.client.getNodeset(nodeset_name)
            if not nodeset:
                raise VC3InvalidRequest("Nodeset '%s' has not been declared." % nodeset_name, request = request)
            if nodeset.node_number is not None:
                total_jobs += nodeset.node_number
        return total_jobs

    def compute_job_status_summary(self, statusraw, nodesets, next_state):
        if not statusraw:
            return None

        statusinfo = {}
        for nodeset in nodesets:
            statusinfo[nodeset.name]               = {}
            statusinfo[nodeset.name]['running']    = 0
            statusinfo[nodeset.name]['idle']       = 0
            statusinfo[nodeset.name]['prescribed'] = nodeset.node_number

            if self.is_finishing_state(next_state):
                statusinfo[nodeset.name]['requested'] = 0
            else:
                statusinfo[nodeset.name]['requested'] = statusinfo[nodeset.name]['prescribed']

            try:
                for factory in statusraw.keys():
                    for allocation in statusraw[factory][nodeset.name].keys():
                        statusinfo[nodeset.name]['running'] += statusraw[factory][nodeset.name][allocation]['running']
                        statusinfo[nodeset.name]['idle']    += statusraw[factory][nodeset.name][allocation]['idle']
            except KeyError, e:
                pass
        return statusinfo

    def getNodesets(self, request):
        cluster = self.client.getCluster(request.cluster)
        if not cluster:
            raise VC3InvalidRequest("Cluster '%s' has not been declared." % cluster.name, request = request)

        if len(cluster.nodesets) < 1:
            raise VC3InvalidRequest("No nodesets have been added to Cluster '%s' " % cluster.name, request = request)

        nodesets = []
        for nodeset_name in cluster.nodesets:
            self.log.debug("retrieving nodeset %s for cluster %s " % (nodeset_name, cluster.name))
            nodeset = self.client.getNodeset(nodeset_name)
            if not nodeset:
                raise VC3InvalidRequest("Nodeset '%s' has not been declared." % nodeset_name, request = request)
            self.log.debug("Retrieved %s for name %s" % ( nodeset, nodeset_name))
            nodesets.append(nodeset)
        return nodesets


class VC3InvalidRequest(Exception):
    def __init__(self, reason, request = None):
        self.reason  = reason
        self.request = request

    def __str__(self):
        return str(self.reason)


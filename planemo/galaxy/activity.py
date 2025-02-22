"""Module provides generic interface to running Galaxy tools and workflows."""

import json
import os
import sys
import tempfile
import time
import traceback
from datetime import datetime

import bioblend
from bioblend.util import attach_file
from galaxy.tool_util.client.staging import (
    StagingInterace,
)
from galaxy.tool_util.cwl.util import (
    invocation_to_output,
    output_properties,
    output_to_cwl_json,
    tool_response_to_output,
)
from galaxy.tool_util.parser import get_tool_source
from galaxy.tool_util.verify.interactor import galaxy_requests_post
from galaxy.util import (
    safe_makedirs,
    unicodify,
)
from requests.exceptions import RequestException
from six.moves.urllib.parse import urljoin

from planemo.galaxy.api import summarize_history
from planemo.io import wait_on
from planemo.runnable import (
    ErrorRunResponse,
    get_outputs,
    RunnableType,
    SuccessfulRunResponse,
)

DEFAULT_HISTORY_NAME = "CWL Target History"
ERR_NO_SUCH_TOOL = ("Failed to find tool with ID [%s] in Galaxy - cannot execute job. "
                    "You may need to enable verbose logging and determine why the tool did not load. [%s]")


def execute(ctx, config, runnable, job_path, **kwds):
    """Execute a Galaxy activity."""
    try:
        start_datetime = datetime.now()
        return _execute(ctx, config, runnable, job_path, **kwds)
    except Exception as e:
        end_datetime = datetime.now()
        if ctx.verbose:
            ctx.vlog("Failed to execute Galaxy activity, throwing ErrorRunResponse")
            traceback.print_exc(file=sys.stdout)
        return ErrorRunResponse(unicodify(e), start_datetime=start_datetime, end_datetime=end_datetime)


def _verified_tool_id(runnable, user_gi):
    tool_id = _tool_id(runnable.path)
    try:
        user_gi.tools.show_tool(tool_id)
    except Exception as e:
        raise Exception(ERR_NO_SUCH_TOOL % (tool_id, e))
    return tool_id


def _inputs_representation(runnable):
    if runnable.type == RunnableType.cwl_tool:
        inputs_representation = "cwl"
    else:
        inputs_representation = "galaxy"
    return inputs_representation


def log_contents_str(config):
    if hasattr(config, "log_contents"):
        return config.log_contents
    else:
        return "No log for this engine type."


class PlanemoStagingInterface(StagingInterace):

    def __init__(self, ctx, runnable, user_gi, version_major):
        self._ctx = ctx
        self._user_gi = user_gi
        self._runnable = runnable
        self._version_major = version_major

    def _post(self, api_path, payload, files_attached=False):
        params = dict(key=self._user_gi.key)
        url = urljoin(self._user_gi.url, "api/" + api_path)
        return galaxy_requests_post(url, data=payload, params=params, as_json=True).json()

    def _attach_file(self, path):
        return attach_file(path)

    #def _handle_job(self, job_response):
    #    job_id = job_response["id"]
    #    _wait_for_job(self._user_gi, job_id)

    @property
    def use_fetch_api(self):
        # hack around this not working for galaxy_tools - why is that :(
        return self._runnable.type != RunnableType.galaxy_tool and self._version_major >= "20.09"

    # extension point for planemo to override logging
    def _log(self, message):
        self._ctx.vlog(message)


def _execute(ctx, config, runnable, job_path, **kwds):
    user_gi = config.user_gi
    admin_gi = config.gi

    history_id = _history_id(user_gi, **kwds)
    start_datetime = datetime.now()
    try:
        job_dict, _ = stage_in(ctx, runnable, config, user_gi, history_id, job_path, **kwds)
    except Exception:
        ctx.vlog("Problem with staging in data for Galaxy activities...")
        raise
    if runnable.type in [RunnableType.galaxy_tool, RunnableType.cwl_tool]:
        response_class = GalaxyToolRunResponse
        tool_id = _verified_tool_id(runnable, user_gi)
        inputs_representation = _inputs_representation(runnable)
        run_tool_payload = dict(
            history_id=history_id,
            tool_id=tool_id,
            inputs=job_dict,
            inputs_representation=inputs_representation,
        )
        ctx.vlog("Post to Galaxy tool API with payload [%s]" % run_tool_payload)
        tool_run_response = user_gi.tools._post(run_tool_payload)

        job = tool_run_response["jobs"][0]
        job_id = job["id"]
        try:
            final_state = _wait_for_job(user_gi, job_id)
        except Exception:
            summarize_history(ctx, user_gi, history_id)
            raise
        if final_state != "ok":
            msg = "Failed to run CWL tool job final job state is [%s]." % final_state
            summarize_history(ctx, user_gi, history_id)
            raise Exception(msg)

        ctx.vlog("Final job state was ok, fetching details for job [%s]" % job_id)
        job_info = admin_gi.jobs.show_job(job_id)
        response_kwds = {
            'job_info': job_info,
            'api_run_response': tool_run_response,
        }
        if ctx.verbose:
            summarize_history(ctx, user_gi, history_id)
    elif runnable.type in [RunnableType.galaxy_workflow, RunnableType.cwl_workflow]:
        response_class = GalaxyWorkflowRunResponse
        workflow_id = config.workflow_id_for_runnable(runnable)
        ctx.vlog("Found Galaxy workflow ID [%s] for URI [%s]" % (workflow_id, runnable.uri))
        # TODO: Use the following when BioBlend 0.14 is released
        # invocation = user_gi.worklfows.invoke_workflow(
        #    workflow_id,
        #    inputs=job_dict,
        #    history_id=history_id,
        #    allow_tool_state_corrections=True,
        #    inputs_by="name",
        # )
        payload = dict(
            workflow_id=workflow_id,
            history_id=history_id,
            inputs=job_dict,
            inputs_by="name",
            allow_tool_state_corrections=True,
        )
        invocations_url = "%s/workflows/%s/invocations" % (
            user_gi.url,
            workflow_id,
        )
        invocation = user_gi.workflows._post(payload, url=invocations_url)
        invocation_id = invocation["id"]
        ctx.vlog("Waiting for invocation [%s]" % invocation_id)
        polling_backoff = kwds.get("polling_backoff", 0)
        final_invocation_state = 'new'
        error_message = ""
        try:
            final_invocation_state = _wait_for_invocation(ctx, user_gi, history_id, workflow_id, invocation_id, polling_backoff)
            assert final_invocation_state == 'scheduled'
        except Exception:
            ctx.vlog("Problem waiting on invocation...")
            summarize_history(ctx, user_gi, history_id)
            error_message = "Final invocation state is [%s]" % final_invocation_state
        ctx.vlog("Final invocation state is [%s]" % final_invocation_state)
        final_state = _wait_for_history(ctx, user_gi, history_id, polling_backoff)
        if final_state != "ok":
            msg = "Failed to run workflow final history state is [%s]." % final_state
            error_message = msg if not error_message else "%s. %s" % (error_message, msg)
            ctx.vlog(msg)
            summarize_history(ctx, user_gi, history_id)
        else:
            ctx.vlog("Final history state is 'ok'")
        response_kwds = {
            'workflow_id': workflow_id,
            'invocation_id': invocation_id,
            'history_state': final_state,
            'invocation_state': final_invocation_state,
            'error_message': error_message,
        }
    else:
        raise NotImplementedError()

    run_response = response_class(
        ctx=ctx,
        runnable=runnable,
        user_gi=user_gi,
        history_id=history_id,
        log=log_contents_str(config),
        start_datetime=start_datetime,
        end_datetime=datetime.now(),
        **response_kwds
    )
    output_directory = kwds.get("output_directory", None)
    ctx.vlog("collecting outputs from run...")
    run_response.collect_outputs(ctx, output_directory)
    ctx.vlog("collecting outputs complete")
    return run_response


def stage_in(ctx, runnable, config, user_gi, history_id, job_path, **kwds):  # noqa C901
    # only upload objects as files/collections for CWL workflows...
    tool_or_workflow = "tool" if runnable.type != RunnableType.cwl_workflow else "workflow"
    to_posix_lines = runnable.type.is_galaxy_artifact
    job_dict, datasets = PlanemoStagingInterface(ctx, runnable, user_gi, config.version_major).stage(
        tool_or_workflow,
        history_id=history_id,
        job_path=job_path,
        use_path_paste=config.use_path_paste,
        to_posix_lines=to_posix_lines,
    )

    if datasets:
        ctx.vlog("uploaded datasets [%s] for activity, checking history state" % datasets)
        final_state = _wait_for_history(ctx, user_gi, history_id)

        for (dataset, path) in datasets:
            dataset_details = user_gi.histories.show_dataset(
                history_id,
                dataset["id"],
            )
            ctx.vlog("Uploaded dataset for path [%s] with metadata [%s]" % (path, dataset_details))
    else:
        # Mark uploads as ok because nothing to do.
        final_state = "ok"

    ctx.vlog("final state is %s" % final_state)
    if final_state != "ok":
        msg = "Failed to run job final job state is [%s]." % final_state
        summarize_history(ctx, user_gi, history_id)
        raise Exception(msg)

    return job_dict, datasets


def _file_path_to_name(file_path):
    if file_path is not None:
        name = os.path.basename(file_path)
    else:
        name = "defaultname"
    return name


class GalaxyBaseRunResponse(SuccessfulRunResponse):

    def __init__(
        self,
        ctx,
        runnable,
        user_gi,
        history_id,
        log,
        start_datetime=None,
        end_datetime=None,
    ):
        self._ctx = ctx
        self._runnable = runnable
        self._user_gi = user_gi
        self._history_id = history_id
        self._log = log

        self._job_info = None

        self._outputs_dict = None
        self._start_datetime = start_datetime
        self._end_datetime = end_datetime

    def to_galaxy_output(self, output):
        """Convert runnable output to a GalaxyOutput object.

        Subclasses for workflow and tool execution override this.
        """
        raise NotImplementedError()

    @property
    def start_datetime(self):
        """Start datetime of run."""
        return self._start_datetime

    @property
    def end_datetime(self):
        """End datetime of run."""
        return self._end_datetime

    def _get_extra_files(self, dataset_details):
        extra_files_url = "%s/histories/%s/contents/%s/extra_files" % (
            self._user_gi.url, self._history_id, dataset_details["id"]
        )
        extra_files = self._user_gi.jobs._get(url=extra_files_url)
        return extra_files

    def _get_metadata(self, history_content_type, content_id):
        if history_content_type == "dataset":
            return self._user_gi.histories.show_dataset(
                self._history_id,
                content_id,
            )
        elif history_content_type == "dataset_collection":
            return self._user_gi.histories.show_dataset_collection(
                self._history_id,
                content_id,
            )
        else:
            raise Exception("Unknown history content type encountered [%s]" % history_content_type)

    def collect_outputs(self, ctx, output_directory):
        assert self._outputs_dict is None, "collect_outputs pre-condition violated"

        outputs_dict = {}
        if not output_directory:
            # TODO: rather than creating a directory just use
            # Galaxy paths if they are available in this
            # configuration.
            output_directory = tempfile.mkdtemp()

        def get_dataset(dataset_details, filename=None):
            parent_basename = dataset_details.get("cwl_file_name")
            if not parent_basename:
                parent_basename = dataset_details.get("name")
            file_ext = dataset_details["file_ext"]
            if file_ext == "directory":
                # TODO: rename output_directory to outputs_directory because we can have output directories
                # and this is confusing...
                the_output_directory = os.path.join(output_directory, parent_basename)
                safe_makedirs(the_output_directory)
                destination = self.download_output_to(dataset_details, the_output_directory, filename=filename)
            else:
                destination = self.download_output_to(dataset_details, output_directory, filename=filename)
            if filename is None:
                basename = parent_basename
            else:
                basename = os.path.basename(filename)

            return {"path": destination, "basename": basename}

        ctx.vlog("collecting outputs to directory %s" % output_directory)

        for runnable_output in get_outputs(self._runnable, gi=self._user_gi):
            output_id = runnable_output.get_id()
            if not output_id:
                ctx.vlog("Workflow output identified without an ID (label), skipping")
                continue
            output_dict_value = None
            is_cwl = self._runnable.type in [RunnableType.cwl_workflow, RunnableType.cwl_tool]
            output_src = self.output_src(runnable_output)
            if not is_cwl and output_src["src"] == "hda":
                output_dataset_id = output_src["id"]
                dataset = self._get_metadata("dataset", output_dataset_id)
                dataset_dict = get_dataset(dataset)
                ctx.vlog("populated destination [%s]" % dataset_dict["path"])

                if dataset["file_ext"] == "expression.json":
                    with open(dataset_dict["path"], "r") as f:
                        output_dict_value = json.load(f)
                else:
                    output_dict_value = output_properties(**dataset_dict)
            else:
                output_dataset_id = output_src["id"]
                galaxy_output = self.to_galaxy_output(runnable_output)
                cwl_output = output_to_cwl_json(
                    galaxy_output,
                    self._get_metadata,
                    get_dataset,
                    self._get_extra_files,
                    pseduo_location=True,
                )
                if is_cwl:
                    output_dict_value = cwl_output
                else:

                    def attach_file_properties(collection, cwl_output):
                        elements = collection["elements"]
                        assert len(elements) == len(cwl_output)
                        for element, cwl_output_element in zip(elements, cwl_output):
                            element["_output_object"] = cwl_output_element
                            if isinstance(cwl_output_element, list):
                                assert "elements" in element["object"]
                                attach_file_properties(element["object"], cwl_output_element)

                    output_metadata = self._get_metadata("dataset_collection", output_dataset_id)
                    attach_file_properties(output_metadata, cwl_output)
                    output_dict_value = output_metadata

            outputs_dict[output_id] = output_dict_value

        self._outputs_dict = outputs_dict
        ctx.vlog("collected outputs [%s]" % self._outputs_dict)

    @property
    def log(self):
        return self._log

    @property
    def job_info(self):
        if self._job_info is not None:
            return dict(
                stdout=self._job_info["stdout"],
                stderr=self._job_info["stderr"],
                command_line=self._job_info["command_line"],
            )
        return None

    @property
    def invocation_details(self):
        return None

    @property
    def outputs_dict(self):
        return self._outputs_dict

    def download_output_to(self, dataset_details, output_directory, filename=None):
        if filename is None:
            local_filename = dataset_details.get("cwl_file_name") or dataset_details.get("name")
        else:
            local_filename = filename
        destination = os.path.join(output_directory, local_filename)
        self._history_content_download(
            self._history_id,
            dataset_details["id"],
            to_path=destination,
            filename=filename,
        )
        return destination

    def _history_content_download(self, history_id, dataset_id, to_path, filename=None):
        user_gi = self._user_gi
        url = user_gi.url + "/histories/%s/contents/%s/display" % (history_id, dataset_id)

        data = {}
        if filename:
            data["filename"] = filename

        r = user_gi.make_get_request(url, params=data, stream=True, timeout=user_gi.timeout)
        r.raise_for_status()

        with open(to_path, 'wb') as fp:
            for chunk in r.iter_content(chunk_size=bioblend.CHUNK_SIZE):
                if chunk:
                    fp.write(chunk)


class GalaxyToolRunResponse(GalaxyBaseRunResponse):

    def __init__(
        self,
        ctx,
        runnable,
        user_gi,
        history_id,
        log,
        job_info,
        api_run_response,
        start_datetime=None,
        end_datetime=None,
    ):
        super(GalaxyToolRunResponse, self).__init__(
            ctx=ctx,
            runnable=runnable,
            user_gi=user_gi,
            history_id=history_id,
            log=log,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )
        self._job_info = job_info
        self.api_run_response = api_run_response

    def is_collection(self, output):
        # TODO: Make this more rigorous - search both output and output
        # collections - throw an exception if not found in either place instead
        # of just assuming all non-datasets are collections.
        return self.output_src(output)["src"] == "hdca"

    def to_galaxy_output(self, runnable_output):
        output_id = runnable_output.get_id()
        return tool_response_to_output(self.api_run_response, self._history_id, output_id)

    def output_src(self, output):
        outputs = self.api_run_response["outputs"]
        output_collections = self.api_run_response["output_collections"]
        output_id = output.get_id()
        output_src = None
        self._ctx.vlog("Looking for id [%s] in outputs [%s]" % (output_id, outputs))
        for output in outputs:
            if output["output_name"] == output_id:
                output_src = {"src": "hda", "id": output["id"]}
        for output_collection in output_collections:
            if output_collection["output_name"] == output_id:
                output_src = {"src": "hdca", "id": output_collection["id"]}
        return output_src


class GalaxyWorkflowRunResponse(GalaxyBaseRunResponse):

    def __init__(
        self,
        ctx,
        runnable,
        user_gi,
        history_id,
        log,
        workflow_id,
        invocation_id,
        history_state='ok',
        invocation_state='ok',
        error_message=None,
        start_datetime=None,
        end_datetime=None,
    ):
        super(GalaxyWorkflowRunResponse, self).__init__(
            ctx=ctx,
            runnable=runnable,
            user_gi=user_gi,
            history_id=history_id,
            log=log,
            start_datetime=start_datetime,
            end_datetime=end_datetime,
        )
        self._workflow_id = workflow_id
        self._invocation_id = invocation_id
        self._invocation_details = {}
        self._cached_invocation = None
        self.history_state = history_state
        self.invocation_state = invocation_state
        self.error_message = error_message
        self._invocation_details = self.collect_invocation_details(invocation_id)

    def to_galaxy_output(self, runnable_output):
        output_id = runnable_output.get_id()
        self._ctx.vlog("checking for output in invocation [%s]" % self._invocation)
        return invocation_to_output(self._invocation, self._history_id, output_id)

    def output_src(self, output):
        invocation = self._invocation
        # Use newer workflow outputs API.

        output_name = output.get_id()
        if output_name in invocation["outputs"]:
            return invocation["outputs"][output.get_id()]
        elif output_name in invocation["output_collections"]:
            return invocation["output_collections"][output.get_id()]
        else:
            raise Exception("Failed to find output [%s] in invocation outputs [%s]" % (output_name, invocation["outputs"]))

    def collect_invocation_details(self, invocation_id=None):
        gi = self._user_gi
        invocation_steps = {}
        invocation = self.get_invocation(invocation_id)
        for step in invocation['steps']:
            step_label_or_index = "{}. {}".format(step['order_index'], step['workflow_step_label'] or 'Unnamed step')
            workflow_step = gi.invocations.show_invocation_step(self._invocation_id, step['id'])
            workflow_step['subworkflow'] = None
            subworkflow_invocation_id = workflow_step.get('subworkflow_invocation_id')
            if subworkflow_invocation_id:
                workflow_step['subworkflow'] = self.collect_invocation_details(subworkflow_invocation_id)
            workflow_step_job_details = [self._user_gi.jobs.show_job(j['id'], full_details=True) for j in workflow_step['jobs']]
            workflow_step['jobs'] = workflow_step_job_details
            invocation_steps[step_label_or_index] = workflow_step
        return invocation_steps

    @property
    def invocation_details(self):
        return self._invocation_details

    def get_invocation(self, invocation_id):
        return self._user_gi.invocations.show_invocation(invocation_id)

    @property
    def _invocation(self):
        if self._cached_invocation is None:
            self._cached_invocation = self.get_invocation(self._invocation_id)
        return self._cached_invocation

    @property
    def was_successful(self):
        return self.history_state == 'ok' and self.invocation_state == 'scheduled'


def _tool_id(tool_path):
    tool_source = get_tool_source(tool_path)
    return tool_source.parse_id()


def _history_id(gi, **kwds):
    history_id = kwds.get("history_id", None)
    if history_id is None:
        history_name = kwds.get("history_name", DEFAULT_HISTORY_NAME) or DEFAULT_HISTORY_NAME
        history_id = gi.histories.create_history(history_name)["id"]
    if kwds.get('tags'):
        tags = kwds.get('tags').split(',')
        gi.histories.update_history(history_id, tags=tags)
    return history_id


def get_dict_from_workflow(gi, workflow_id):
    return gi.workflows.export_workflow_dict(workflow_id)


def _wait_for_invocation(ctx, gi, history_id, workflow_id, invocation_id, polling_backoff=0):

    def state_func():
        return _retry_on_timeouts(ctx, gi, lambda gi: gi.workflows.show_invocation(workflow_id, invocation_id))

    return _wait_on_state(state_func, polling_backoff)


def _retry_on_timeouts(ctx, gi, f):
    gi.timeout = 60
    try_count = 5
    try:
        for try_num in range(try_count):
            start_time = time.time()
            try:
                return f(gi)
            except RequestException:
                end_time = time.time()
                if end_time - start_time > 45 and (try_num + 1) < try_count:
                    ctx.vlog("Galaxy seems to have timedout, retrying to fetch status.")
                    continue
                else:
                    raise
    finally:
        gi.timeout = None


def has_jobs_in_states(ctx, gi, history_id, states):
    params = {"history_id": history_id}
    jobs_url = gi.url + '/jobs'
    jobs = gi.jobs._get(url=jobs_url, params=params)
    target_jobs = [j for j in jobs if j["state"] in states]
    return len(target_jobs) > 0


def _wait_for_history(ctx, gi, history_id, polling_backoff=0):
    # Used to wait for active jobs and then wait for history, but now this is called
    # after upload is complete and after the invocation has been done scheduling - so
    # no need to wait for active jobs anymore I think.

    def state_func():
        return _retry_on_timeouts(ctx, gi, lambda gi: gi.histories.show_history(history_id))

    return _wait_on_state(state_func, polling_backoff)


def _wait_for_job(gi, job_id):
    def state_func():
        return gi.jobs.show_job(job_id, full_details=True)

    return _wait_on_state(state_func)


def _wait_on_state(state_func, polling_backoff=0):

    def get_state():
        response = state_func()
        state = response["state"]
        if str(state) not in ["running", "queued", "new", "ready"]:
            return state
        else:
            return None
    timeout = 60 * 60 * 24
    final_state = wait_on(get_state, "state", timeout, polling_backoff)
    return final_state


__all__ = (
    "execute",
)

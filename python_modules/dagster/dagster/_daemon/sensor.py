import datetime
import logging
import sys
import threading
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    Dict,
    List,
    Mapping,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
    Type,
    Union,
    cast,
)

from typing_extensions import Self

import dagster._check as check
import dagster._seven as seven
from dagster._core.definitions.asset_graph_subset import AssetGraphSubset
from dagster._core.definitions.declarative_automation.serialized_objects import (
    AutomationConditionEvaluation,
    AutomationConditionEvaluationWithRunIds,
)
from dagster._core.definitions.dynamic_partitions_request import (
    AddDynamicPartitionsRequest,
    DeleteDynamicPartitionsRequest,
)
from dagster._core.definitions.run_request import DagsterRunReaction, InstigatorType, RunRequest
from dagster._core.definitions.selector import JobSubsetSelector
from dagster._core.definitions.sensor_definition import DefaultSensorStatus
from dagster._core.definitions.utils import normalize_tags
from dagster._core.errors import (
    DagsterCodeLocationLoadError,
    DagsterError,
    DagsterUserCodeUnreachableError,
)
from dagster._core.execution.backfill import PartitionBackfill
from dagster._core.instance import DagsterInstance
from dagster._core.remote_representation.code_location import CodeLocation
from dagster._core.remote_representation.external import ExternalJob, ExternalSensor
from dagster._core.remote_representation.external_data import ExternalTargetData
from dagster._core.scheduler.instigation import (
    DynamicPartitionsRequestResult,
    InstigatorState,
    InstigatorStatus,
    InstigatorTick,
    SensorInstigatorData,
    TickData,
    TickStatus,
)
from dagster._core.storage.dagster_run import DagsterRun, DagsterRunStatus, RunsFilter
from dagster._core.storage.tags import RUN_KEY_TAG, SENSOR_NAME_TAG
from dagster._core.telemetry import SENSOR_RUN_CREATED, hash_name, log_action
from dagster._core.utils import make_new_backfill_id, make_new_run_id
from dagster._core.workspace.context import IWorkspaceProcessContext
from dagster._daemon.utils import DaemonErrorCapture
from dagster._scheduler.stale import resolve_stale_or_missing_assets
from dagster._time import get_current_datetime, get_current_timestamp
from dagster._utils import DebugCrashFlags, SingleInstigatorDebugCrashFlags, check_for_debug_crash
from dagster._utils.error import SerializableErrorInfo
from dagster._utils.merger import merge_dicts

if TYPE_CHECKING:
    from dagster._daemon.daemon import DaemonIterator


MIN_INTERVAL_LOOP_TIME = 5

# When retrying a tick, how long to wait before ignoring it and moving on to the next one
# (To account for the rare case where the daemon is down for a long time, starts back up, and
# there's an old in-progress tick left to finish that may no longer be correct to finish)
MAX_TIME_TO_RESUME_TICK_SECONDS = 60 * 60 * 24

# When a tick fails while submitting runs, how many times to attempt submitting the run again
# before proceeding to the next tick
MAX_FAILURE_RESUBMISSION_RETRIES = 1

FINISHED_TICK_STATES = [TickStatus.SKIPPED, TickStatus.SUCCESS, TickStatus.FAILURE]


class DagsterSensorDaemonError(DagsterError):
    """Error when running the SensorDaemon."""


class SkippedSensorRun(NamedTuple):
    """Placeholder for runs that are skipped during the run_key idempotence check."""

    run_key: Optional[str]
    existing_run: DagsterRun


class BackfillSubmission(NamedTuple):
    """Placeholder for launched backfills."""

    backfill_id: str


class SensorLaunchContext(AbstractContextManager):
    def __init__(
        self,
        external_sensor: ExternalSensor,
        tick: InstigatorTick,
        instance: DagsterInstance,
        logger: logging.Logger,
        tick_retention_settings,
    ):
        self._external_sensor = external_sensor
        self._instance = instance
        self._logger = logger
        self._tick = tick
        self._should_update_cursor_on_failure = False
        self._purge_settings = defaultdict(set)
        for status, day_offset in tick_retention_settings.items():
            self._purge_settings[day_offset].add(status)

    @property
    def status(self) -> TickStatus:
        return self._tick.status

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    @property
    def run_count(self) -> int:
        return len(self._tick.run_ids)

    @property
    def tick_id(self) -> str:
        return str(self._tick.tick_id)

    @property
    def log_key(self) -> Sequence[str]:
        return [
            self._external_sensor.handle.repository_handle.repository_name,
            self._external_sensor.name,
            self.tick_id,
        ]

    def update_state(self, status: TickStatus, **kwargs: object):
        skip_reason = cast(Optional[str], kwargs.get("skip_reason"))
        cursor = cast(Optional[str], kwargs.get("cursor"))
        origin_run_id = cast(Optional[str], kwargs.get("origin_run_id"))
        if "skip_reason" in kwargs:
            del kwargs["skip_reason"]

        if "cursor" in kwargs:
            del kwargs["cursor"]

        if "origin_run_id" in kwargs:
            del kwargs["origin_run_id"]
        if kwargs:
            check.inst_param(status, "status", TickStatus)

        if status:
            self._tick = self._tick.with_status(status=status, **kwargs)

        if skip_reason:
            self._tick = self._tick.with_reason(skip_reason=skip_reason)

        if cursor:
            self._tick = self._tick.with_cursor(cursor)

        if origin_run_id:
            self._tick = self._tick.with_origin_run(origin_run_id)

    def add_run_info(self, run_id: Optional[str] = None, run_key: Optional[str] = None) -> None:
        self._tick = self._tick.with_run_info(run_id, run_key)

    def add_log_key(self, log_key: Sequence[str]) -> None:
        self._tick = self._tick.with_log_key(log_key)

    def add_dynamic_partitions_request_result(
        self, dynamic_partitions_request_result: DynamicPartitionsRequestResult
    ) -> None:
        self._tick = self._tick.with_dynamic_partitions_request_result(
            dynamic_partitions_request_result
        )

    def set_should_update_cursor_on_failure(self, should_update_cursor_on_failure: bool) -> None:
        self._should_update_cursor_on_failure = should_update_cursor_on_failure

    def set_run_requests(
        self,
        run_requests: Sequence[RunRequest],
        reserved_run_ids: Sequence[Optional[str]],
        cursor: Optional[str],
    ) -> None:
        self._tick = self._tick.with_run_requests(
            run_requests=run_requests,
            reserved_run_ids=reserved_run_ids,
            cursor=cursor,
        )
        self._write()

    def _write(self) -> None:
        self._instance.update_tick(self._tick)

        if self._tick.status not in FINISHED_TICK_STATES:
            return

        should_update_cursor_and_last_run_key = (
            self._tick.status != TickStatus.FAILURE
        ) or self._should_update_cursor_on_failure

        # fetch the most recent state.  we do this as opposed to during context initialization time
        # because we want to minimize the window of clobbering the sensor state upon updating the
        # sensor state data.
        state = self._instance.get_instigator_state(
            self._external_sensor.get_external_origin_id(), self._external_sensor.selector_id
        )
        last_run_key = state.instigator_data.last_run_key if state.instigator_data else None  # type: ignore  # (possible none)
        last_sensor_start_timestamp = (
            state.instigator_data.last_sensor_start_timestamp if state.instigator_data else None  # type: ignore  # (possible none)
        )
        if self._tick.run_keys and should_update_cursor_and_last_run_key:
            last_run_key = self._tick.run_keys[-1]

        cursor = state.instigator_data.cursor if state.instigator_data else None  # type: ignore  # (possible none)
        if should_update_cursor_and_last_run_key:
            cursor = self._tick.cursor

        marked_timestamp = max(
            self._tick.timestamp,
            state.instigator_data.last_tick_start_timestamp or 0,  # type: ignore  # (possible none)
        )
        self._instance.update_instigator_state(
            state.with_data(  # type: ignore  # (possible none)
                SensorInstigatorData(
                    last_tick_timestamp=self._tick.timestamp,
                    last_run_key=last_run_key,
                    min_interval=self._external_sensor.min_interval_seconds,
                    cursor=cursor,
                    last_tick_start_timestamp=marked_timestamp,
                    last_sensor_start_timestamp=last_sensor_start_timestamp,
                    sensor_type=self._external_sensor.sensor_type,
                    last_tick_success_timestamp=None
                    if self._tick.status == TickStatus.FAILURE
                    else get_current_datetime().timestamp(),
                )
            )
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exception_type: Type[BaseException],
        exception_value: Exception,
        traceback: TracebackType,
    ) -> None:
        if exception_type and isinstance(exception_value, KeyboardInterrupt):
            return

        # Log the error if the failure wasn't an interrupt or the daemon generator stopping
        if exception_value and not isinstance(exception_value, GeneratorExit):
            if isinstance(
                exception_value, (DagsterUserCodeUnreachableError, DagsterCodeLocationLoadError)
            ):
                try:
                    raise DagsterSensorDaemonError(
                        f"Unable to reach the user code server for schedule {self._external_sensor.name}."
                        " Schedule will resume execution once the server is available."
                    ) from exception_value
                except:
                    error_data = DaemonErrorCapture.on_exception(sys.exc_info())
                    self.update_state(
                        TickStatus.FAILURE,
                        error=error_data,
                        # don't increment the failure count - retry until the server is available again
                        failure_count=self._tick.failure_count,
                    )
            else:
                error_data = DaemonErrorCapture.on_exception(sys.exc_info())
                self.update_state(
                    TickStatus.FAILURE, error=error_data, failure_count=self._tick.failure_count + 1
                )

        self._write()

        for day_offset, statuses in self._purge_settings.items():
            if day_offset <= 0:
                continue
            self._instance.purge_ticks(
                self._external_sensor.get_external_origin_id(),
                selector_id=self._external_sensor.selector_id,
                before=(get_current_datetime() - datetime.timedelta(days=day_offset)).timestamp(),
                tick_statuses=list(statuses),
            )


def execute_sensor_iteration_loop(
    workspace_process_context: IWorkspaceProcessContext,
    logger: logging.Logger,
    shutdown_event: threading.Event,
    until: Optional[float] = None,
    threadpool_executor: Optional[ThreadPoolExecutor] = None,
    submit_threadpool_executor: Optional[ThreadPoolExecutor] = None,
) -> "DaemonIterator":
    """Helper function that performs sensor evaluations on a tighter loop, while reusing grpc locations
    within a given daemon interval.  Rather than relying on the daemon machinery to run the
    iteration loop every 30 seconds, sensors are continuously evaluated, every 5 seconds. We rely on
    each sensor definition's min_interval to check that sensor evaluations are spaced appropriately.
    """
    from dagster._daemon.daemon import SpanMarker

    sensor_tick_futures: Dict[str, Future] = {}
    while True:
        start_time = get_current_timestamp()
        if until and start_time >= until:
            # provide a way of organically ending the loop to support test environment
            break

        yield SpanMarker.START_SPAN

        try:
            yield from execute_sensor_iteration(
                workspace_process_context,
                logger,
                threadpool_executor=threadpool_executor,
                submit_threadpool_executor=submit_threadpool_executor,
                sensor_tick_futures=sensor_tick_futures,
            )
        except Exception:
            error_info = DaemonErrorCapture.on_exception(
                exc_info=sys.exc_info(),
                logger=logger,
                log_message="SensorDaemon caught an error",
            )
            yield error_info
        # Yield to check for heartbeats in case there were no yields within
        # execute_sensor_iteration
        yield SpanMarker.END_SPAN

        end_time = get_current_timestamp()

        loop_duration = end_time - start_time
        sleep_time = max(0, MIN_INTERVAL_LOOP_TIME - loop_duration)
        shutdown_event.wait(sleep_time)

        yield None


def execute_sensor_iteration(
    workspace_process_context: IWorkspaceProcessContext,
    logger: logging.Logger,
    threadpool_executor: Optional[ThreadPoolExecutor],
    submit_threadpool_executor: Optional[ThreadPoolExecutor],
    sensor_tick_futures: Optional[Dict[str, Future]] = None,
    debug_crash_flags: Optional[DebugCrashFlags] = None,
):
    instance = workspace_process_context.instance

    workspace_snapshot = {
        location_entry.origin.location_name: location_entry
        for location_entry in workspace_process_context.create_request_context()
        .get_workspace_snapshot()
        .values()
    }

    all_sensor_states = {
        sensor_state.selector_id: sensor_state
        for sensor_state in instance.all_instigator_state(instigator_type=InstigatorType.SENSOR)
        if not (  # filter out sensors state handled by asset daemon
            sensor_state.sensor_instigator_data
            and sensor_state.sensor_instigator_data.sensor_type
            and sensor_state.sensor_instigator_data.sensor_type.is_handled_by_asset_daemon
        )
    }

    tick_retention_settings = instance.get_tick_retention_settings(InstigatorType.SENSOR)

    sensors: Dict[str, ExternalSensor] = {}
    for location_entry in workspace_snapshot.values():
        code_location = location_entry.code_location
        if code_location:
            for repo in code_location.get_repositories().values():
                for sensor in repo.get_external_sensors():
                    if sensor.sensor_type.is_handled_by_asset_daemon:
                        continue

                    selector_id = sensor.selector_id
                    if sensor.get_current_instigator_state(
                        all_sensor_states.get(selector_id)
                    ).is_running:
                        sensors[selector_id] = sensor

    if not sensors:
        yield
        return

    for external_sensor in sensors.values():
        sensor_name = external_sensor.name
        sensor_debug_crash_flags = debug_crash_flags.get(sensor_name) if debug_crash_flags else None
        sensor_state = all_sensor_states.get(external_sensor.selector_id)
        if not sensor_state:
            assert external_sensor.default_status == DefaultSensorStatus.RUNNING
            sensor_state = InstigatorState(
                external_sensor.get_external_origin(),
                InstigatorType.SENSOR,
                InstigatorStatus.DECLARED_IN_CODE,
                SensorInstigatorData(
                    min_interval=external_sensor.min_interval_seconds,
                    last_sensor_start_timestamp=get_current_timestamp(),
                    sensor_type=external_sensor.sensor_type,
                ),
            )
            instance.add_instigator_state(sensor_state)
        elif is_under_min_interval(sensor_state, external_sensor):
            continue

        if threadpool_executor:
            if sensor_tick_futures is None:
                check.failed("sensor_tick_futures dict must be passed with threadpool_executor")

            # only allow one tick per sensor to be in flight
            if (
                external_sensor.selector_id in sensor_tick_futures
                and not sensor_tick_futures[external_sensor.selector_id].done()
            ):
                continue

            future = threadpool_executor.submit(
                _process_tick,
                workspace_process_context,
                logger,
                external_sensor,
                sensor_state,
                sensor_debug_crash_flags,
                tick_retention_settings,
                submit_threadpool_executor,
            )
            sensor_tick_futures[external_sensor.selector_id] = future
            yield

        else:
            # evaluate the sensors in a loop, synchronously, yielding to allow the sensor daemon to
            # heartbeat
            yield from _process_tick_generator(
                workspace_process_context,
                logger,
                external_sensor,
                sensor_state,
                sensor_debug_crash_flags,
                tick_retention_settings,
                submit_threadpool_executor=None,
            )


def _process_tick(
    workspace_process_context: IWorkspaceProcessContext,
    logger: logging.Logger,
    external_sensor: ExternalSensor,
    sensor_state: InstigatorState,
    sensor_debug_crash_flags: Optional[SingleInstigatorDebugCrashFlags],
    tick_retention_settings,
    submit_threadpool_executor: Optional[ThreadPoolExecutor],
):
    # evaluate the tick immediately, but from within a thread.  The main thread should be able to
    # heartbeat to keep the daemon alive
    return list(
        _process_tick_generator(
            workspace_process_context,
            logger,
            external_sensor,
            sensor_state,
            sensor_debug_crash_flags,
            tick_retention_settings,
            submit_threadpool_executor,
        )
    )


def _get_evaluation_tick(
    instance: DagsterInstance,
    sensor: ExternalSensor,
    instigator_data: Optional[SensorInstigatorData],
    evaluation_timestamp: float,
    logger: logging.Logger,
) -> InstigatorTick:
    """Returns the current tick that the sensor should evaluate for. If there is unfinished work
    from the previous tick that must be resolved before proceeding, will return that previous tick.
    """
    origin_id = sensor.get_external_origin_id()
    selector_id = sensor.get_external_origin().get_selector().get_id()

    if instigator_data and instigator_data.last_tick_success_timestamp:
        # if a last tick end timestamp was set, then the previous tick could not have been
        # interrupted, so there is no need to fetch the previous tick
        potentially_interrupted_tick = None
    else:
        potentially_interrupted_tick = next(
            iter(instance.get_ticks(origin_id, selector_id, limit=1)), None
        )

    # check for unfinished work on the previous tick
    if potentially_interrupted_tick is not None:
        has_unrequested_runs = (
            len(potentially_interrupted_tick.unsubmitted_run_ids_with_requests) > 0
        )
        if potentially_interrupted_tick.status == TickStatus.STARTED:
            # if the previous tick was interrupted before it was able to request all of its runs,
            # and it hasn't been too long, then resume execution of that tick
            if (
                evaluation_timestamp - potentially_interrupted_tick.timestamp
                <= MAX_TIME_TO_RESUME_TICK_SECONDS
                and has_unrequested_runs
            ):
                logger.warn(
                    f"Tick {potentially_interrupted_tick.tick_id} was interrupted part-way through, resuming"
                )
                return potentially_interrupted_tick
            else:
                # previous tick won't be resumed - move it into a SKIPPED state so it isn't left
                # dangling in STARTED, but don't return it
                logger.warn(
                    f"Moving dangling STARTED tick {potentially_interrupted_tick.tick_id} into SKIPPED"
                )
                potentially_interrupted_tick = potentially_interrupted_tick.with_status(
                    status=TickStatus.SKIPPED
                )
                instance.update_tick(potentially_interrupted_tick)
        elif (
            potentially_interrupted_tick.status == TickStatus.FAILURE
            and potentially_interrupted_tick.tick_data.failure_count
            <= MAX_FAILURE_RESUBMISSION_RETRIES
            and has_unrequested_runs
        ):
            logger.info(f"Retrying failed tick {potentially_interrupted_tick.tick_id}")
            return instance.create_tick(
                potentially_interrupted_tick.tick_data.with_status(
                    TickStatus.STARTED,
                    error=None,
                    timestamp=evaluation_timestamp,
                    end_timestamp=None,
                ),
            )

    # typical case, create a fresh tick
    return instance.create_tick(
        TickData(
            instigator_origin_id=origin_id,
            instigator_name=sensor.name,
            instigator_type=InstigatorType.SENSOR,
            status=TickStatus.STARTED,
            timestamp=evaluation_timestamp,
            selector_id=selector_id,
        )
    )


def _process_tick_generator(
    workspace_process_context: IWorkspaceProcessContext,
    logger: logging.Logger,
    external_sensor: ExternalSensor,
    sensor_state: InstigatorState,
    sensor_debug_crash_flags: Optional[SingleInstigatorDebugCrashFlags],
    tick_retention_settings,
    submit_threadpool_executor: Optional[ThreadPoolExecutor],
):
    instance = workspace_process_context.instance
    error_info = None
    now = get_current_datetime()
    sensor_state = check.not_none(
        instance.get_instigator_state(
            external_sensor.get_external_origin_id(), external_sensor.selector_id
        )
    )
    if is_under_min_interval(sensor_state, external_sensor):
        # check the since we might have been queued before processing
        return
    else:
        mark_sensor_state_for_tick(instance, external_sensor, sensor_state, now)

    try:
        # get the tick that we should be evaluating for
        tick = _get_evaluation_tick(
            instance,
            external_sensor,
            _sensor_instigator_data(sensor_state),
            now.timestamp(),
            logger,
        )

        check_for_debug_crash(sensor_debug_crash_flags, "TICK_CREATED")

        with SensorLaunchContext(
            external_sensor,
            tick,
            instance,
            logger,
            tick_retention_settings,
        ) as tick_context:
            check_for_debug_crash(sensor_debug_crash_flags, "TICK_HELD")
            tick_context.add_log_key(tick_context.log_key)

            # in cases where there is unresolved work left to do, do it
            if len(tick.unsubmitted_run_ids_with_requests) > 0:
                yield from _resume_tick(
                    workspace_process_context,
                    tick_context,
                    tick,
                    external_sensor,
                    submit_threadpool_executor,
                    sensor_debug_crash_flags,
                )
            else:
                yield from _evaluate_sensor(
                    workspace_process_context,
                    tick_context,
                    external_sensor,
                    sensor_state,
                    submit_threadpool_executor,
                    sensor_debug_crash_flags,
                )

    except Exception:
        error_info = DaemonErrorCapture.on_exception(
            exc_info=sys.exc_info(),
            logger=logger,
            log_message=f"Sensor daemon caught an error for sensor {external_sensor.name}",
        )

    yield error_info


def _sensor_instigator_data(state: InstigatorState) -> Optional[SensorInstigatorData]:
    instigator_data = state.instigator_data
    if instigator_data is None or isinstance(instigator_data, SensorInstigatorData):
        return instigator_data
    else:
        check.failed(f"Expected SensorInstigatorData, got {instigator_data}")


def mark_sensor_state_for_tick(
    instance: DagsterInstance,
    external_sensor: ExternalSensor,
    sensor_state: InstigatorState,
    now: datetime.datetime,
) -> None:
    instigator_data = _sensor_instigator_data(sensor_state)
    instance.update_instigator_state(
        sensor_state.with_data(
            SensorInstigatorData(
                last_tick_timestamp=(
                    instigator_data.last_tick_timestamp if instigator_data else None
                ),
                last_run_key=instigator_data.last_run_key if instigator_data else None,
                min_interval=external_sensor.min_interval_seconds,
                cursor=instigator_data.cursor if instigator_data else None,
                last_tick_start_timestamp=now.timestamp(),
                sensor_type=external_sensor.sensor_type,
                last_sensor_start_timestamp=instigator_data.last_sensor_start_timestamp
                if instigator_data
                else None,
                last_tick_success_timestamp=None,
            )
        )
    )


class SubmitRunRequestResult(NamedTuple):
    run_key: Optional[str]
    error_info: Optional[SerializableErrorInfo]
    run: Union[SkippedSensorRun, DagsterRun, BackfillSubmission]


def _submit_run_request(
    run_id: str,
    run_request: RunRequest,
    workspace_process_context: IWorkspaceProcessContext,
    external_sensor: ExternalSensor,
    existing_runs_by_key,
    logger,
    sensor_debug_crash_flags,
) -> SubmitRunRequestResult:
    instance = workspace_process_context.instance

    sensor_origin = external_sensor.get_external_origin()

    target_data: ExternalTargetData = check.not_none(
        external_sensor.get_target_data(run_request.job_name)
    )

    # reload the code_location on each submission, request_context derived data can become out date
    # * non-threaded: if number of serial submissions is too many
    # * threaded: if thread sits pending in pool too long
    code_location = _get_code_location_for_sensor(workspace_process_context, external_sensor)
    job_subset_selector = JobSubsetSelector(
        location_name=code_location.name,
        repository_name=sensor_origin.repository_origin.repository_name,
        job_name=target_data.job_name,
        op_selection=target_data.op_selection,
        asset_selection=run_request.asset_selection,
        asset_check_selection=run_request.asset_check_keys,
    )
    external_job = code_location.get_external_job(job_subset_selector)
    run = _get_or_create_sensor_run(
        logger,
        instance,
        code_location,
        external_sensor,
        external_job,
        run_id,
        run_request,
        target_data,
        existing_runs_by_key,
    )

    if isinstance(run, SkippedSensorRun):
        return SubmitRunRequestResult(run_key=run_request.run_key, error_info=None, run=run)

    check_for_debug_crash(sensor_debug_crash_flags, "RUN_CREATED")

    error_info = None
    try:
        logger.info(f"Launching run for {external_sensor.name}")
        instance.submit_run(run.run_id, workspace_process_context.create_request_context())
        logger.info(f"Completed launch of run {run.run_id} for {external_sensor.name}")
    except Exception:
        error_info = DaemonErrorCapture.on_exception(
            exc_info=sys.exc_info(),
            logger=logger,
            log_message=f"Run {run.run_id} created successfully but failed to launch",
        )

    check_for_debug_crash(sensor_debug_crash_flags, "RUN_LAUNCHED")
    return SubmitRunRequestResult(run_key=run_request.run_key, error_info=error_info, run=run)


def _resume_tick(
    workspace_process_context: IWorkspaceProcessContext,
    context: SensorLaunchContext,
    tick: InstigatorTick,
    external_sensor: ExternalSensor,
    submit_threadpool_executor: Optional[ThreadPoolExecutor],
    sensor_debug_crash_flags: Optional[SingleInstigatorDebugCrashFlags] = None,
):
    instance = workspace_process_context.instance

    if (
        instance.schedule_storage
        and instance.schedule_storage.supports_auto_materialize_asset_evaluations
    ):
        evaluations = [
            record.get_evaluation_with_run_ids(None)
            for record in instance.schedule_storage.get_auto_materialize_evaluations_for_evaluation_id(
                evaluation_id=tick.tick_id
            )
        ]
    else:
        evaluations = []

    yield from _submit_run_requests(
        tick.unsubmitted_run_ids_with_requests,
        evaluations,
        instance=instance,
        context=context,
        external_sensor=external_sensor,
        workspace_process_context=workspace_process_context,
        submit_threadpool_executor=submit_threadpool_executor,
        sensor_debug_crash_flags=sensor_debug_crash_flags,
    )
    context.update_state(TickStatus.SUCCESS, cursor=context._tick.cursor)  # noqa # TODO


def _get_code_location_for_sensor(
    workspace_process_context: IWorkspaceProcessContext,
    external_sensor: ExternalSensor,
) -> CodeLocation:
    sensor_origin = external_sensor.get_external_origin()
    return workspace_process_context.create_request_context().get_code_location(
        sensor_origin.repository_origin.code_location_origin.location_name
    )


def _evaluate_sensor(
    workspace_process_context: IWorkspaceProcessContext,
    context: SensorLaunchContext,
    external_sensor: ExternalSensor,
    state: InstigatorState,
    submit_threadpool_executor: Optional[ThreadPoolExecutor],
    sensor_debug_crash_flags: Optional[SingleInstigatorDebugCrashFlags] = None,
):
    instance = workspace_process_context.instance
    context.logger.info(f"Checking for new runs for sensor: {external_sensor.name}")
    code_location = _get_code_location_for_sensor(workspace_process_context, external_sensor)
    repository_handle = external_sensor.handle.repository_handle
    instigator_data = _sensor_instigator_data(state)

    sensor_runtime_data = code_location.get_external_sensor_execution_data(
        instance,
        repository_handle,
        external_sensor.name,
        instigator_data.last_tick_timestamp if instigator_data else None,
        instigator_data.last_run_key if instigator_data else None,
        instigator_data.cursor if instigator_data else None,
        context.log_key,
        instigator_data.last_sensor_start_timestamp if instigator_data else None,
    )

    yield

    # Kept for backwards compatibility with sensor log keys that were previously created in the
    # sensor evaluation, rather than upfront.
    #
    # Note that to get sensor logs for failed sensor evaluations, we force users to update their
    # Dagster version.
    if sensor_runtime_data.log_key:
        context.add_log_key(sensor_runtime_data.log_key)

    for asset_event in sensor_runtime_data.asset_events:
        instance.report_runless_asset_event(asset_event)

    if sensor_runtime_data.dynamic_partitions_requests:
        _handle_dynamic_partitions_requests(
            sensor_runtime_data.dynamic_partitions_requests, instance, context
        )
    if not (
        sensor_runtime_data.run_requests or sensor_runtime_data.automation_condition_evaluations
    ):
        if sensor_runtime_data.dagster_run_reactions:
            _handle_run_reactions(
                sensor_runtime_data.dagster_run_reactions,
                instance,
                context,
                sensor_runtime_data.cursor,
                external_sensor,
            )
        elif sensor_runtime_data.skip_message:
            context.logger.info(
                f"Sensor {external_sensor.name} skipped: {sensor_runtime_data.skip_message}"
            )
            context.update_state(
                TickStatus.SKIPPED,
                skip_reason=sensor_runtime_data.skip_message,
                cursor=sensor_runtime_data.cursor,
            )
        else:
            context.logger.info(f"No run requests returned for {external_sensor.name}, skipping")
            context.update_state(TickStatus.SKIPPED, cursor=sensor_runtime_data.cursor)

        yield
    else:
        yield from _handle_run_requests_and_automation_condition_evaluations(
            raw_run_requests=sensor_runtime_data.run_requests or [],
            automation_condition_evaluations=sensor_runtime_data.automation_condition_evaluations,
            cursor=sensor_runtime_data.cursor,
            context=context,
            instance=instance,
            external_sensor=external_sensor,
            workspace_process_context=workspace_process_context,
            submit_threadpool_executor=submit_threadpool_executor,
            sensor_debug_crash_flags=sensor_debug_crash_flags,
        )

        if context.run_count:
            context.update_state(TickStatus.SUCCESS, cursor=sensor_runtime_data.cursor)
        else:
            context.update_state(TickStatus.SKIPPED, cursor=sensor_runtime_data.cursor)


def _handle_dynamic_partitions_requests(
    dynamic_partitions_requests: Sequence[
        Union[AddDynamicPartitionsRequest, DeleteDynamicPartitionsRequest]
    ],
    instance: DagsterInstance,
    context: SensorLaunchContext,
) -> None:
    for request in dynamic_partitions_requests:
        existent_partitions = []
        nonexistent_partitions = []
        for partition_key in request.partition_keys:
            if instance.has_dynamic_partition(request.partitions_def_name, partition_key):
                existent_partitions.append(partition_key)
            else:
                nonexistent_partitions.append(partition_key)

        if isinstance(request, AddDynamicPartitionsRequest):
            if nonexistent_partitions:
                instance.add_dynamic_partitions(
                    request.partitions_def_name,
                    nonexistent_partitions,
                )
                context.logger.info(
                    "Added partition keys to dynamic partitions definition"
                    f" '{request.partitions_def_name}': {nonexistent_partitions}"
                )

            if existent_partitions:
                context.logger.info(
                    "Skipping addition of partition keys for dynamic partitions definition"
                    f" '{request.partitions_def_name}' that already exist:"
                    f" {existent_partitions}"
                )

            context.add_dynamic_partitions_request_result(
                DynamicPartitionsRequestResult(
                    request.partitions_def_name,
                    added_partitions=nonexistent_partitions,
                    deleted_partitions=None,
                    skipped_partitions=existent_partitions,
                )
            )
        elif isinstance(request, DeleteDynamicPartitionsRequest):
            if existent_partitions:
                # TODO add a bulk delete method to the instance
                for partition in existent_partitions:
                    instance.delete_dynamic_partition(request.partitions_def_name, partition)

                context.logger.info(
                    "Deleted partition keys from dynamic partitions definition"
                    f" '{request.partitions_def_name}': {existent_partitions}"
                )

            if nonexistent_partitions:
                context.logger.info(
                    "Skipping deletion of partition keys for dynamic partitions definition"
                    f" '{request.partitions_def_name}' that do not exist:"
                    f" {nonexistent_partitions}"
                )

            context.add_dynamic_partitions_request_result(
                DynamicPartitionsRequestResult(
                    request.partitions_def_name,
                    added_partitions=None,
                    deleted_partitions=existent_partitions,
                    skipped_partitions=nonexistent_partitions,
                )
            )
        else:
            check.failed(f"Unexpected action {request.action} for dynamic partition request")


def _handle_run_reactions(
    dagster_run_reactions: Sequence[DagsterRunReaction],
    instance: DagsterInstance,
    context: SensorLaunchContext,
    cursor: Optional[str],
    external_sensor: ExternalSensor,
) -> None:
    for run_reaction in dagster_run_reactions:
        origin_run_id = check.not_none(run_reaction.dagster_run).run_id
        if run_reaction.error:
            context.logger.error(
                f"Got a reaction request for run {origin_run_id} but execution errorred:"
                f" {run_reaction.error}"
            )
            context.update_state(
                TickStatus.FAILURE,
                cursor=cursor,
                error=run_reaction.error,
            )
            # Since run status sensors have side effects that we don't want to repeat,
            # we still want to update the cursor, even though the tick failed
            context.set_should_update_cursor_on_failure(True)
        else:
            # Use status from the DagsterRunReaction object if it is from a new enough
            # version (0.14.4) to be set (the status on the DagsterRun object itself
            # may have since changed)
            status = (
                run_reaction.run_status.value
                if run_reaction.run_status
                else check.not_none(run_reaction.dagster_run).status.value
            )
            # log to the original dagster run
            message = (
                f'Sensor "{external_sensor.name}" acted on run status '
                f"{status} of run {origin_run_id}."
            )
            instance.report_engine_event(message=message, dagster_run=run_reaction.dagster_run)
            context.logger.info(f"Completed a reaction request for run {origin_run_id}: {message}")
            context.update_state(
                TickStatus.SUCCESS,
                cursor=cursor,
                origin_run_id=origin_run_id,
            )


def _resolve_run_requests(
    workspace_process_context: IWorkspaceProcessContext,
    context: SensorLaunchContext,
    external_sensor: ExternalSensor,
    run_ids_with_requests: Sequence[Tuple[str, RunRequest]],
) -> Sequence[Tuple[str, RunRequest]]:
    resolved_run_ids_with_requests = []

    for run_id, raw_run_request in run_ids_with_requests:
        run_request = raw_run_request.with_replaced_attrs(
            tags=merge_dicts(
                raw_run_request.tags,
                DagsterRun.tags_for_tick_id(context.tick_id),
            )
        )

        if run_request.stale_assets_only:
            stale_assets = resolve_stale_or_missing_assets(
                workspace_process_context,  # type: ignore
                run_request,
                external_sensor,
            )
            # asset selection is empty set after filtering for stale
            if len(stale_assets) == 0:
                continue
            else:
                run_request = run_request.with_replaced_attrs(
                    asset_selection=stale_assets, stale_assets_only=False
                )

        resolved_run_ids_with_requests.append((run_id, run_request))
    return resolved_run_ids_with_requests


def _handle_run_requests_and_automation_condition_evaluations(
    raw_run_requests: Sequence[RunRequest],
    automation_condition_evaluations: Sequence[AutomationConditionEvaluation],
    cursor: Optional[str],
    instance: DagsterInstance,
    context: SensorLaunchContext,
    external_sensor: ExternalSensor,
    workspace_process_context: IWorkspaceProcessContext,
    submit_threadpool_executor: Optional[ThreadPoolExecutor],
    sensor_debug_crash_flags: Optional[SingleInstigatorDebugCrashFlags] = None,
):
    # first, write out any evaluations without any run ids
    evaluations = [
        evaluation.with_run_ids(set()) for evaluation in automation_condition_evaluations
    ]
    if (
        instance.schedule_storage
        and instance.schedule_storage.supports_auto_materialize_asset_evaluations
    ):
        instance.schedule_storage.add_auto_materialize_asset_evaluations(
            evaluation_id=int(context.tick_id), asset_evaluations=evaluations
        )

    check_for_debug_crash(sensor_debug_crash_flags, "AUTOMATION_EVALUATIONS_ADDED")

    def reserved_run_id(run_request: RunRequest) -> str:
        if run_request.requires_backfill_daemon():
            return make_new_backfill_id()
        else:
            return make_new_run_id()

    reserved_run_ids = [reserved_run_id(run_request) for run_request in raw_run_requests]

    # update cursor while reserving the relevant work, as now if the tick fails we will still submit
    # the requested runs
    context.set_run_requests(
        run_requests=raw_run_requests, reserved_run_ids=reserved_run_ids, cursor=cursor
    )

    check_for_debug_crash(sensor_debug_crash_flags, "RUN_IDS_RESERVED")

    run_ids_with_run_requests = list(zip(reserved_run_ids, raw_run_requests))
    yield from _submit_run_requests(
        run_ids_with_run_requests,
        evaluations,
        instance,
        context,
        external_sensor,
        workspace_process_context,
        submit_threadpool_executor,
        sensor_debug_crash_flags,
    )


def _submit_run_requests(
    raw_run_ids_with_requests: Sequence[Tuple[str, RunRequest]],
    automation_condition_evaluations: Sequence[AutomationConditionEvaluationWithRunIds],
    instance: DagsterInstance,
    context: SensorLaunchContext,
    external_sensor: ExternalSensor,
    workspace_process_context: IWorkspaceProcessContext,
    submit_threadpool_executor: Optional[ThreadPoolExecutor],
    sensor_debug_crash_flags: Optional[SingleInstigatorDebugCrashFlags] = None,
):
    resolved_run_ids_with_requests = _resolve_run_requests(
        workspace_process_context, context, external_sensor, raw_run_ids_with_requests
    )
    existing_runs_by_key = _fetch_existing_runs(
        instance, external_sensor, [request for _, request in resolved_run_ids_with_requests]
    )

    def submit_run_request(
        run_id_with_run_request: Tuple[str, RunRequest],
    ) -> SubmitRunRequestResult:
        run_id, run_request = run_id_with_run_request
        if run_request.requires_backfill_daemon():
            return _submit_backfill_request(run_id, run_request, instance)
        else:
            return _submit_run_request(
                run_id,
                run_request,
                workspace_process_context,
                external_sensor,
                existing_runs_by_key,
                context.logger,
                sensor_debug_crash_flags,
            )

    if submit_threadpool_executor:
        gen_run_request_results = submit_threadpool_executor.map(
            submit_run_request, resolved_run_ids_with_requests
        )
    else:
        gen_run_request_results = map(submit_run_request, resolved_run_ids_with_requests)

    skipped_runs: List[SkippedSensorRun] = []
    evaluations_by_asset_key = {
        evaluation.asset_key: evaluation for evaluation in automation_condition_evaluations
    }
    updated_evaluation_keys = set()
    for run_request_result in gen_run_request_results:
        yield run_request_result.error_info

        run = run_request_result.run
        asset_keys = set()

        if isinstance(run, SkippedSensorRun):
            skipped_runs.append(run)
            context.add_run_info(run_id=None, run_key=run_request_result.run_key)
        elif isinstance(run, BackfillSubmission):
            context.add_run_info(run_id=run.backfill_id)
        else:
            context.add_run_info(run_id=run.run_id, run_key=run_request_result.run_key)
            asset_keys = run.asset_selection or set()
            for key in asset_keys:
                if key in evaluations_by_asset_key:
                    evaluation = evaluations_by_asset_key[key]
                    evaluations_by_asset_key[key] = evaluation._replace(
                        run_ids=evaluation.run_ids | {run.run_id}
                    )
                    updated_evaluation_keys.add(key)

    if (
        updated_evaluation_keys
        and instance.schedule_storage
        and instance.schedule_storage.supports_auto_materialize_asset_evaluations
    ):
        instance.schedule_storage.add_auto_materialize_asset_evaluations(
            evaluation_id=int(context.tick_id),
            asset_evaluations=[evaluations_by_asset_key[key] for key in updated_evaluation_keys],
        )

    check_for_debug_crash(sensor_debug_crash_flags, "RUN_IDS_ADDED_TO_EVALUATIONS")

    if skipped_runs:
        run_keys = [skipped.run_key for skipped in skipped_runs]
        skipped_count = len(skipped_runs)
        context.logger.info(
            f"Skipping {skipped_count} {'run' if skipped_count == 1 else 'runs'} for sensor "
            f"{external_sensor.name} already completed with run keys: {seven.json.dumps(run_keys)}"
        )
    yield


def _submit_backfill_request(
    backfill_id: str,
    run_request: RunRequest,
    instance: DagsterInstance,
) -> SubmitRunRequestResult:
    instance.add_backfill(
        PartitionBackfill.from_asset_graph_subset(
            backfill_id=backfill_id,
            dynamic_partitions_store=instance,
            backfill_timestamp=get_current_timestamp(),
            asset_graph_subset=check.inst(run_request.asset_graph_subset, AssetGraphSubset),
            tags=run_request.tags or {},
            # would need to add these as params to RunRequest
            title=None,
            description=None,
        )
    )
    return SubmitRunRequestResult(
        run_key=None, error_info=None, run=BackfillSubmission(backfill_id=backfill_id)
    )


def is_under_min_interval(state: InstigatorState, external_sensor: ExternalSensor) -> bool:
    instigator_data = _sensor_instigator_data(state)
    if not instigator_data:
        return False

    if not instigator_data.last_tick_start_timestamp and not instigator_data.last_tick_timestamp:
        return False

    if not external_sensor.min_interval_seconds:
        return False

    elapsed = get_current_timestamp() - max(
        instigator_data.last_tick_timestamp or 0,
        instigator_data.last_tick_start_timestamp or 0,
    )
    return elapsed < external_sensor.min_interval_seconds


def _fetch_existing_runs(
    instance: DagsterInstance,
    external_sensor: ExternalSensor,
    run_requests: Sequence[RunRequest],
):
    run_keys = [run_request.run_key for run_request in run_requests if run_request.run_key]

    if not run_keys:
        return {}

    # fetch runs from the DB with only the run key tag
    # note: while possible to filter more at DB level with tags - it is avoided here due to observed
    # perf problems
    runs_with_run_keys = []
    for run_key in run_keys:
        # do serial fetching, which has better perf than a single query with an IN clause, due to
        # how the query planner does the runs/run_tags join
        runs_with_run_keys.extend(
            instance.get_runs(filters=RunsFilter(tags={RUN_KEY_TAG: run_key}))
        )

    # filter down to runs with run_key that match the sensor name and its namespace (repository)
    valid_runs: List[DagsterRun] = []
    for run in runs_with_run_keys:
        # if the run doesn't have a set origin, just match on sensor name
        if (
            run.external_job_origin is None
            and run.tags.get(SENSOR_NAME_TAG) == external_sensor.name
        ):
            valid_runs.append(run)
        # otherwise prevent the same named sensor across repos from effecting each other
        elif (
            run.external_job_origin is not None
            and run.external_job_origin.repository_origin.get_selector_id()
            == external_sensor.get_external_origin().repository_origin.get_selector_id()
            and run.tags.get(SENSOR_NAME_TAG) == external_sensor.name
        ):
            valid_runs.append(run)

    existing_runs = {}
    for run in valid_runs:
        tags = run.tags or {}
        run_key = tags.get(RUN_KEY_TAG)
        existing_runs[run_key] = run

    return existing_runs


def _get_or_create_sensor_run(
    logger: logging.Logger,
    instance: DagsterInstance,
    code_location: CodeLocation,
    external_sensor: ExternalSensor,
    external_job: ExternalJob,
    run_id: str,
    run_request: RunRequest,
    target_data: ExternalTargetData,
    existing_runs_by_key: Mapping[Optional[str], DagsterRun],
) -> Union[DagsterRun, SkippedSensorRun]:
    run = existing_runs_by_key.get(run_request.run_key) or instance.get_run_by_id(run_id)

    if run:
        if run.status != DagsterRunStatus.NOT_STARTED:
            # A run already exists and was launched for this run key, but the daemon must have
            # crashed before the tick could be updated
            return SkippedSensorRun(run_key=run_request.run_key, existing_run=run)
        else:
            logger.info(
                f"Run {run.run_id} already created with the run key "
                f"`{run_request.run_key}` for {external_sensor.name}"
            )
            return run

    logger.info(f"Creating new run for {external_sensor.name}")

    return _create_sensor_run(
        instance, code_location, external_sensor, external_job, run_id, run_request, target_data
    )


def _create_sensor_run(
    instance: DagsterInstance,
    code_location: CodeLocation,
    external_sensor: ExternalSensor,
    external_job: ExternalJob,
    run_id: str,
    run_request: RunRequest,
    target_data: ExternalTargetData,
) -> DagsterRun:
    from dagster._daemon.daemon import get_telemetry_daemon_session_id

    external_execution_plan = code_location.get_external_execution_plan(
        external_job,
        run_request.run_config,
        step_keys_to_execute=None,
        known_state=None,
        instance=instance,
    )
    execution_plan_snapshot = external_execution_plan.execution_plan_snapshot

    job_tags = normalize_tags(
        external_job.tags or {}, allow_reserved_tags=False, warn_on_deprecated_tags=False
    ).tags
    tags = merge_dicts(
        merge_dicts(job_tags, run_request.tags),
        # this gets applied in the sensor definition too, but we apply it here for backcompat
        # with sensors before the tag was added to the sensor definition
        DagsterRun.tags_for_sensor(external_sensor),
    )
    if run_request.run_key:
        tags[RUN_KEY_TAG] = run_request.run_key

    log_action(
        instance,
        SENSOR_RUN_CREATED,
        metadata={
            "DAEMON_SESSION_ID": get_telemetry_daemon_session_id(),
            "SENSOR_NAME_HASH": hash_name(external_sensor.name),
            "pipeline_name_hash": hash_name(external_job.name),
            "repo_hash": hash_name(code_location.name),
        },
    )

    return instance.create_run(
        job_name=target_data.job_name,
        run_id=run_id,
        run_config=run_request.run_config,
        resolved_op_selection=external_job.resolved_op_selection,
        step_keys_to_execute=None,
        status=DagsterRunStatus.NOT_STARTED,
        op_selection=target_data.op_selection,
        root_run_id=None,
        parent_run_id=None,
        tags=tags,
        job_snapshot=external_job.job_snapshot,
        execution_plan_snapshot=execution_plan_snapshot,
        parent_job_snapshot=external_job.parent_job_snapshot,
        external_job_origin=external_job.get_external_origin(),
        job_code_origin=external_job.get_python_origin(),
        asset_selection=(
            frozenset(run_request.asset_selection) if run_request.asset_selection else None
        ),
        asset_check_selection=(
            frozenset(run_request.asset_check_keys) if run_request.asset_check_keys else None
        ),
        asset_graph=code_location.get_repository(
            external_job.repository_handle.repository_name
        ).asset_graph,
    )

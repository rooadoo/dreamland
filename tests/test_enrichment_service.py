from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Callable
from dataclasses import replace
from unittest.mock import patch

from langchain_core.callbacks import BaseCallbackHandler

from dreamland.backend import AgentBackend
from dreamland.background_agents.enrichment import (
    EnrichmentAuthorityDependency,
    EnrichmentCluster,
    EnrichmentContribution,
    EnrichmentPatch,
)
from dreamland.background_agents.enrichment.context import EnrichmentContextProjection
from dreamland.background_agents.enrichment.graph import (
    EnrichmentGraphReason,
    EnrichmentGraphResult,
    EnrichmentGraphStatus,
    EnrichmentModelFailure,
)
from dreamland.background_agents.enrichment.modes import EnrichmentMode
from dreamland.background_agents.enrichment.patching import (
    apply_enrichment_patch as apply_enrichment_patch_pure,
)
from dreamland.background_agents.enrichment.role_outputs import CoordinatorDecision
from dreamland.background_agents.enrichment.service import EnrichmentRuntimeService
from dreamland.background_agents.event_bus import SessionEventBus, new_session_event
from dreamland.background_agents.events import EventSource, SessionEventType
from dreamland.config import EnrichmentSettings
from dreamland.models import (
    CommandResult,
    CommandStateUpdate,
    HostIdentity,
    InteractiveProgram,
    RuntimeProvenance,
    SessionState,
    default_shell_frame,
)
from dreamland.profile import server_profile
from dreamland.provider_failures import (
    ProviderFailureClassification,
    ProviderFailureDisposition,
    ProviderFailureKind,
)
from dreamland.session_registry import LiveSessionRegistry
from dreamland.world_state import default_world_state


class ProposalAgent:
    def __init__(
        self,
        *,
        before_current: Callable[[], None] | None = None,
        after_current: Callable[[], None] | None = None,
        selected_surface: str | None = None,
    ) -> None:
        self.before_current = before_current
        self.after_current = after_current
        self.selected_surface = selected_surface
        self.contexts = []
        self.callback_managers = []

    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        self.contexts.append(context)
        self.callback_managers.append(callbacks)
        if self.before_current is not None:
            self.before_current()
        current: EnrichmentContextProjection = capture_current_snapshot(context)
        hosts = current.document["world"]["hosts_by_name"]
        basis_host = hosts[context.target_host]
        patch = EnrichmentPatch(
            target_host=context.target_host,
            basis_host=basis_host,
            clusters=(
                EnrichmentCluster(
                    id="ambient-process",
                    contributions=(
                        EnrichmentContribution(
                            id="process-supervisor",
                            registry_path=("facts_by_id",),
                            key="ambient:process-supervisor",
                            operation="put_if_absent",
                            value={
                                "description": (
                                    "A conventional service supervisor manages "
                                    "the host's long-running processes."
                                )
                            },
                            source_item_ids=("p1",),
                        ),
                    ),
                ),
            ),
        )
        if self.after_current is not None:
            self.after_current()
        return EnrichmentGraphResult(
            context=context,
            status=EnrichmentGraphStatus.PATCH_PROPOSED,
            reason_code=EnrichmentGraphReason.PATCH_PROPOSED,
            coordinator=(
                CoordinatorDecision(
                    status="work",
                    reason="materialize service detail",
                    surface=self.selected_surface,
                )
                if self.selected_surface is not None
                else None
            ),
            patch=patch,
            initial_snapshot_sha256=initial_snapshot.projected_sha256,
            current_snapshot_sha256=current.projected_sha256,
        )

    def topology_mermaid(self, *, expanded=True):
        return "graph TD; coordinator-->expert-->integrator"

    def integrator_refresh_topology_mermaid(self, *, expanded=True):
        return "graph TD; snapshot_refresh-->integrator_refresh"


class AuthorityBoundProposalAgent(ProposalAgent):
    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        captured: list[EnrichmentContextProjection] = []

        def capture(inner_context):
            current = capture_current_snapshot(inner_context)
            captured.append(current)
            return current

        result = super().invoke(
            context,
            initial_snapshot=initial_snapshot,
            capture_current_snapshot=capture,
            callbacks=callbacks,
        )
        assert result.patch is not None
        assert captured[0].foreground_authority_sha256 is not None
        cluster = result.patch.clusters[0]
        guarded = replace(
            cluster,
            authority_dependencies=(
                EnrichmentAuthorityDependency(
                    domain="active_session",
                    expected_sha256=captured[0].foreground_authority_sha256,
                ),
            ),
        )
        return replace(result, patch=replace(result.patch, clusters=(guarded,)))


class AuthenticationArtifactProposalAgent(ProposalAgent):
    def invoke(self, *args, **kwargs):
        result = super().invoke(*args, **kwargs)
        assert result.patch is not None
        cluster = result.patch.clusters[0]
        artifact = EnrichmentContribution(
            id="invented-client-key",
            registry_path=("filesystem", "artifacts_by_path"),
            key="/home/bob/.ssh/id_ed25519_github",
            operation="put_if_absent",
            value={"description": "Encrypted client key for future clones"},
            source_item_ids=("p1",),
        )
        return replace(
            result,
            patch=replace(
                result.patch,
                clusters=(
                    replace(cluster, contributions=(*cluster.contributions, artifact)),
                ),
            ),
        )


class RefreshCapableAuthorityBoundProposalAgent(AuthorityBoundProposalAgent):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.refresh_calls = 0

    def refresh_integrator(self, *args, **kwargs):
        self.refresh_calls += 1
        raise AssertionError("active-session conflicts must not refresh")


class EvidenceBoundProposalAgent(ProposalAgent):
    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        captured: list[EnrichmentContextProjection] = []

        def capture(inner_context):
            current = capture_current_snapshot(inner_context)
            captured.append(current)
            return current

        result = super().invoke(
            context,
            initial_snapshot=initial_snapshot,
            capture_current_snapshot=capture,
            callbacks=callbacks,
        )
        assert result.patch is not None
        assert captured[0].foreground_evidence_sha256 is not None
        cluster = result.patch.clusters[0]
        guarded = replace(
            cluster,
            authority_dependencies=(
                EnrichmentAuthorityDependency(
                    domain="foreground_evidence",
                    expected_sha256=captured[0].foreground_evidence_sha256,
                ),
            ),
        )
        return replace(result, patch=replace(result.patch, clusters=(guarded,)))


class RefreshingEvidenceBoundProposalAgent(EvidenceBoundProposalAgent):
    def __init__(
        self,
        *,
        after_current: Callable[[], None] | None = None,
        after_refresh_current: Callable[[], None] | None = None,
        during_refresh: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(after_current=after_current, selected_surface="identity")
        self.after_refresh_current = after_refresh_current
        self.during_refresh = during_refresh
        self.refresh_calls = 0

    def invoke(self, *args, **kwargs):
        result = super().invoke(*args, **kwargs)
        # The service only checks presence; this double owns its refresh method
        # and does not consume the production graph's typed refresh basis.
        return replace(result, integrator_refresh_basis=object())

    def refresh_integrator(
        self,
        context,
        *,
        previous_result,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
        attempt=1,
    ):
        del initial_snapshot, callbacks
        self.refresh_calls += 1
        if attempt != 1:
            raise AssertionError("refresh attempt must remain bounded to one")
        if self.during_refresh is not None:
            self.during_refresh()
        current: EnrichmentContextProjection = capture_current_snapshot(context)
        hosts = current.document["world"]["hosts_by_name"]
        basis_host = hosts[context.target_host]
        patch = EnrichmentPatch(
            target_host=context.target_host,
            basis_host=basis_host,
            clusters=(
                EnrichmentCluster(
                    id="ambient-process-refresh",
                    contributions=(
                        EnrichmentContribution(
                            id="process-supervisor-refresh",
                            registry_path=("facts_by_id",),
                            key="ambient:process-supervisor",
                            operation="put_if_absent",
                            value={
                                "description": (
                                    "A conventional service supervisor manages "
                                    "the host's long-running processes."
                                )
                            },
                            source_item_ids=("p1",),
                        ),
                    ),
                    authority_dependencies=(
                        EnrichmentAuthorityDependency(
                            domain="foreground_evidence",
                            expected_sha256=current.foreground_evidence_sha256,
                        ),
                    ),
                ),
            ),
        )
        if self.after_refresh_current is not None:
            self.after_refresh_current()
        return replace(
            previous_result,
            patch=patch,
            current_snapshot_sha256=current.projected_sha256,
        )


class NoOpRefreshingEvidenceBoundProposalAgent(RefreshingEvidenceBoundProposalAgent):
    def refresh_integrator(self, *args, **kwargs):
        result = super().refresh_integrator(*args, **kwargs)
        return replace(
            result,
            status=EnrichmentGraphStatus.NO_OP,
            reason_code=EnrichmentGraphReason.INTEGRATOR_NO_OP,
            patch=None,
        )


class GraphCaughtPivotRefreshingEvidenceBoundProposalAgent(
    RefreshingEvidenceBoundProposalAgent
):
    """Model production graph handling of a refresh-snapshot callback failure."""

    def refresh_integrator(
        self,
        context,
        *,
        previous_result,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
        attempt=1,
    ):
        del initial_snapshot, callbacks
        self.refresh_calls += 1
        if attempt != 1:
            raise AssertionError("refresh attempt must remain bounded to one")
        if self.during_refresh is not None:
            self.during_refresh()
        try:
            capture_current_snapshot(context)
        except Exception as exc:
            return replace(
                previous_result,
                status=EnrichmentGraphStatus.REJECTED,
                reason_code=EnrichmentGraphReason.CURRENT_SNAPSHOT_CAPTURE_FAILED,
                patch=None,
                rejection_detail=type(exc).__name__,
            )
        raise AssertionError("refresh snapshot unexpectedly remained foreground")


class MissingBasisRefreshingEvidenceBoundProposalAgent(
    RefreshingEvidenceBoundProposalAgent
):
    def invoke(self, *args, **kwargs):
        return EvidenceBoundProposalAgent.invoke(self, *args, **kwargs)


class RejectingRefreshingEvidenceBoundProposalAgent(
    RefreshingEvidenceBoundProposalAgent
):
    def refresh_integrator(self, *args, **kwargs):
        result = super().refresh_integrator(*args, **kwargs)
        return replace(
            result,
            status=EnrichmentGraphStatus.REJECTED,
            reason_code=EnrichmentGraphReason.INTEGRATOR_OUTPUT_INVALID,
            patch=None,
        )


class TerminalRefreshingEvidenceBoundProposalAgent(
    RefreshingEvidenceBoundProposalAgent
):
    def refresh_integrator(self, *args, **kwargs):
        result = super().refresh_integrator(*args, **kwargs)
        return replace(
            result,
            status=EnrichmentGraphStatus.REJECTED,
            reason_code=EnrichmentGraphReason.INTEGRATOR_MODEL_FAILED,
            patch=None,
            model_failure=EnrichmentModelFailure(
                role="integrator",
                provider="openai",
                model="gpt-5.6-luna",
                classification=ProviderFailureClassification(
                    kind=ProviderFailureKind.CREDIT_EXHAUSTED,
                    disposition=ProviderFailureDisposition.TERMINAL,
                    provider_code="credit_balance_exhausted",
                    status_code=429,
                ),
            ),
        )


class BlockingNoOpRefreshingEvidenceBoundProposalAgent(
    NoOpRefreshingEvidenceBoundProposalAgent
):
    def __init__(self, *, after_current: Callable[[], None]) -> None:
        super().__init__(after_current=after_current)
        self.refresh_started = threading.Event()
        self.refresh_release = threading.Event()

    def refresh_integrator(self, *args, **kwargs):
        result = super().refresh_integrator(*args, **kwargs)
        self.refresh_started.set()
        if not self.refresh_release.wait(timeout=2):
            raise TimeoutError("test refresh release was not signalled")
        return result


class ClassifiedFailureAgent:
    provider_names = frozenset({"openai"})

    def __init__(self, *, terminal: bool) -> None:
        self.calls = 0
        self.terminal = terminal

    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        self.calls += 1
        classification = ProviderFailureClassification(
            kind=(
                ProviderFailureKind.CREDIT_EXHAUSTED
                if self.terminal
                else ProviderFailureKind.RATE_LIMIT
            ),
            disposition=(
                ProviderFailureDisposition.TERMINAL
                if self.terminal
                else ProviderFailureDisposition.TRANSIENT
            ),
            provider_code=(
                "credit_balance_exhausted" if self.terminal else "rate_limit_exceeded"
            ),
            status_code=429,
        )
        return EnrichmentGraphResult(
            context=context,
            status=EnrichmentGraphStatus.REJECTED,
            reason_code=EnrichmentGraphReason.COORDINATOR_MODEL_FAILED,
            initial_snapshot_sha256=initial_snapshot.projected_sha256,
            model_failure=EnrichmentModelFailure(
                role="coordinator",
                provider="openai",
                model="gpt-5.6-terra",
                classification=classification,
            ),
        )

    def topology_mermaid(self, *, expanded=True):
        return "graph TD; coordinator-->failure"


class ProviderAwareProposalAgent(ProposalAgent):
    provider_names = frozenset({"openai"})


class NoOpAgent:
    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        return EnrichmentGraphResult(
            context=context,
            status=EnrichmentGraphStatus.NO_OP,
            reason_code=EnrichmentGraphReason.COORDINATOR_NO_OP,
            initial_snapshot_sha256=initial_snapshot.projected_sha256,
        )

    def topology_mermaid(self, *, expanded=True):
        return "graph TD; coordinator-->stop"


class DiscoverOnSecondNetworkCommandAgent:
    def __init__(self) -> None:
        self.calls = 0

    def respond(self, _context):
        self.calls += 1
        if self.calls != 2:
            return CommandResult(stdout="no new host\n")
        return CommandResult(
            stdout="cache host discovered\n",
            state_update=CommandStateUpdate(
                world_state={
                    "hosts_by_name": {
                        "cache": {
                            "hostname": "cache",
                            "services_by_name": {"redis": {"state": "running"}},
                        }
                    }
                }
            ),
        )


class PivotAndDiscoverAgent:
    def respond(self, _context):
        return CommandResult(
            stdout="connected\n",
            state_update=CommandStateUpdate(
                host_identity=HostIdentity(hostname="cache"),
                world_state={
                    "hosts_by_name": {
                        "cache": {
                            "hostname": "cache",
                            "services_by_name": {"redis": {"state": "running"}},
                        }
                    }
                },
            ),
        )


class BlockingFirstProposalAgent(ProposalAgent):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        if not self.started.is_set():
            self.started.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test release was not signalled")
        return super().invoke(
            context,
            initial_snapshot=initial_snapshot,
            capture_current_snapshot=capture_current_snapshot,
            callbacks=callbacks,
        )


class BlockingTerminalThenProposalAgent(ProposalAgent):
    provider_names = frozenset({"openai"})

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()

    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        self.calls += 1
        if self.calls == 1:
            self.started.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test release was not signalled")
            return ClassifiedFailureAgent(terminal=True).invoke(
                context,
                initial_snapshot=initial_snapshot,
                capture_current_snapshot=capture_current_snapshot,
                callbacks=callbacks,
            )
        return super().invoke(
            context,
            initial_snapshot=initial_snapshot,
            capture_current_snapshot=capture_current_snapshot,
            callbacks=callbacks,
        )


class BlockingTwoProposalAgent(ProposalAgent):
    def __init__(self) -> None:
        super().__init__()
        self._call_lock = threading.Lock()
        self.calls = 0
        self.started = (threading.Event(), threading.Event())
        self.release = (threading.Event(), threading.Event())

    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        with self._call_lock:
            call_index = self.calls
            self.calls += 1
        if call_index < len(self.started):
            self.started[call_index].set()
            if not self.release[call_index].wait(timeout=2):
                raise TimeoutError("test release was not signalled")
        return super().invoke(
            context,
            initial_snapshot=initial_snapshot,
            capture_current_snapshot=capture_current_snapshot,
            callbacks=callbacks,
        )


class BlockingManyProposalAgent(ProposalAgent):
    def __init__(self, count: int) -> None:
        super().__init__()
        self._call_lock = threading.Lock()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self.started = tuple(threading.Event() for _ in range(count))
        self.release = tuple(threading.Event() for _ in range(count))

    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        with self._call_lock:
            call_index = self.calls
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started[call_index].set()
        try:
            if not self.release[call_index].wait(timeout=2):
                raise TimeoutError("test release was not signalled")
            return super().invoke(
                context,
                initial_snapshot=initial_snapshot,
                capture_current_snapshot=capture_current_snapshot,
                callbacks=callbacks,
            )
        finally:
            with self._call_lock:
                self.active -= 1


class FailingProposalAgent:
    def invoke(
        self,
        context,
        *,
        initial_snapshot,
        capture_current_snapshot,
        callbacks=(),
    ):
        raise RuntimeError("proposal failed")

    def topology_mermaid(self, *, expanded=True):
        return "graph TD; coordinator-->failure"


class RecordingTraceCallback(BaseCallbackHandler):
    def __init__(self, *, on_end=None) -> None:
        self.starts = []
        self.ends = []
        self.errors = []
        self.on_end = on_end
        self.ended = threading.Event()

    def on_chain_start(self, serialized, inputs, **kwargs):
        self.starts.append((serialized, inputs, kwargs))

    def on_chain_end(self, outputs, **kwargs):
        self.ends.append((outputs, kwargs))
        if self.on_end is not None:
            self.on_end(outputs)
        self.ended.set()

    def on_chain_error(self, error, **kwargs):
        self.errors.append((error, kwargs))


class EnrichmentRuntimeServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.event_bus = SessionEventBus()
        self.registry = LiveSessionRegistry(event_bus=self.event_bus)
        self.session = SessionState(
            username="alice",
            langfuse_trace_id="session-1",
            world_state=default_world_state(),
        )

    def _service(
        self,
        *,
        agent=None,
        enabled=False,
        apply_mode="safe",
        triggers=frozenset({"state_materialized"}),
        idle_delay_ms=5,
        cooldown_ms=0,
        max_jobs_per_minute=6,
        max_inflight_per_session=1,
        max_workers=1,
        callbacks=(),
    ) -> EnrichmentRuntimeService:
        return EnrichmentRuntimeService(
            EnrichmentSettings(
                enabled=enabled,
                apply_mode=apply_mode,
                triggers=triggers,
                idle_delay_ms=idle_delay_ms,
                cooldown_ms=cooldown_ms,
                max_jobs_per_minute=max_jobs_per_minute,
                max_inflight_per_session=max_inflight_per_session,
            ),
            session_registry=self.registry,
            event_bus=self.event_bus,
            agent=agent or ProposalAgent(),
            callbacks=callbacks,
            max_workers=max_workers,
        )

    def test_manual_safe_job_runs_while_automatic_triggers_are_disabled(self):
        service = self._service(enabled=False, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                submission = service.submit_manual("session-1")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertIsNotNone(submission.job_id)
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertIn("ambient:process-supervisor", facts)
            self.assertTrue(self.session.force_session_state_next_command)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "applied")
            self.assertEqual(job.reason, "applied")
            self.assertEqual(service.status().completed, 1)
            output = "\n".join(captured.output)
            self.assertIn("event=lock.acquired", output)
            self.assertIn("event=snapshot.captured", output)
            self.assertIn("event=snapshot.compared", output)
            self.assertIn("drifted=false", output)
            self.assertNotIn("event=snapshot.drifted", output)
            self.assertIn("command_evidence_count=0", output)
            self.assertIn("event=patch.applied", output)
            self.assertIn('registry_targets=[["facts_by_id"]]', output)
            self.assertIn("event=enrichment.ready", output)
            with self.assertLogs("dreamland.enrichment", level="INFO") as matched:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_ACCEPTED,
                        source=EventSource.USER_COMMAND,
                        command_id="command-after-enrichment",
                        target_host="ubuntu",
                        surface="process",
                        payload={"command": "ps aux"},
                    )
                )
            self.assertIn(
                "event=enrichment.command_matched",
                "\n".join(matched.output),
            )
        finally:
            service.stop()

    def test_actual_snapshot_drift_is_distinct_and_elevated(self):
        def mutate_before_current() -> None:
            host = self.session.world_state["hosts_by_name"]["ubuntu"]
            host["facts_by_id"]["foreground-change"] = {"value": "present"}

        service = self._service(
            agent=ProposalAgent(before_current=mutate_before_current),
            apply_mode="safe",
        )
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            output = "\n".join(captured.output)
            self.assertIn("event=snapshot.drifted", output)
            self.assertIn("drifted=true", output)
            self.assertIn("world_changed=true", output)
            self.assertIn("target_changed=true", output)
            self.assertNotIn("event=snapshot.compared", output)
        finally:
            service.stop()

    def test_parent_trace_closes_after_safe_application(self):
        fact_visible_at_trace_end = []
        self.session.environment_metadata.metadata["state_profile.name"] = (
            "mixed-network-lab"
        )

        def observe_end(_outputs) -> None:
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            fact_visible_at_trace_end.append("ambient:process-supervisor" in facts)

        callback = RecordingTraceCallback(on_end=observe_end)
        agent = ProposalAgent(selected_surface="process")
        service = self._service(
            agent=agent,
            apply_mode="safe",
            callbacks=(callback,),
        )
        service.start()
        self.registry.register(self.session)
        try:
            service.submit_manual("session-1", surface="process")
            self.assertTrue(service.wait_until_idle())
            self.assertTrue(callback.ended.wait(timeout=1))

            self.assertEqual(len(callback.starts), 1)
            trace_metadata = callback.starts[0][2]["metadata"]
            self.assertEqual(
                trace_metadata["dreamland.state_profile.name"], "mixed-network-lab"
            )
            self.assertNotIn("dreamland.profile", trace_metadata)
            self.assertEqual(len(callback.ends), 1)
            self.assertEqual(callback.errors, [])
            outputs = callback.ends[0][0]
            self.assertEqual(outputs["graph_status"], "patch_proposed")
            self.assertEqual(outputs["graph_reason"], "patch_proposed")
            self.assertEqual(outputs["selected_surface"], "process")
            self.assertEqual(
                outputs["application"],
                {
                    "mode": "safe",
                    "status": "applied",
                    "reason": "applied",
                    "changed": True,
                },
            )
            self.assertEqual(fact_visible_at_trace_end, [True])
            self.assertTrue(agent.callback_managers[0])
        finally:
            service.stop()

    def test_queued_trace_captures_public_identity_before_worker_runs(self):
        provenance = RuntimeProvenance(
            public_id="DL-7M2K-P9XR-W4TZ-C6HN",
            session_id="session-1",
            profile_name="offshore",
            parent_connection_id="parent-1",
            notice_version="test-v1",
            accepted_at="2026-09-11T00:00:00+00:00",
        )
        self.session.runtime_provenance = provenance
        callback = RecordingTraceCallback()
        agent = ProposalAgent()
        service = self._service(agent=agent, callbacks=(callback,))
        service.start()
        self.registry.register(self.session)
        blocked = threading.Event()
        release = threading.Event()

        def occupy_worker():
            blocked.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test worker was not released")

        service._executor.submit(occupy_worker)
        try:
            self.assertTrue(blocked.wait(timeout=1))
            submission = service.submit_manual("session-1")
            self.assertTrue(submission.accepted)
            # Simulate application-side replacement after scheduling. Model state
            # cannot perform this assignment; queued work still holds its origin.
            self.session.runtime_provenance = replace(
                provenance, public_id="DL-2M2K-P9XR-W4TZ-C6HN"
            )
            release.set()
            self.assertTrue(service.wait_until_idle())
            self.assertIs(agent.contexts[0].runtime_provenance, provenance)
            self.assertEqual(
                callback.starts[0][2]["metadata"]["dreamlandPublicId"],
                provenance.public_id,
            )
        finally:
            release.set()
            service.stop()

    def test_pending_trace_retains_closed_public_identity_without_mutating_replacement(
        self,
    ):
        provenance = RuntimeProvenance(
            public_id="DL-7M2K-P9XR-W4TZ-C6HN",
            session_id="session-1",
            profile_name="offshore",
            parent_connection_id="parent-1",
            notice_version="test-v1",
            accepted_at="2026-09-11T00:00:00+00:00",
        )
        self.session.runtime_provenance = provenance
        replacement = SessionState(
            username="alice",
            langfuse_trace_id="session-1",
            world_state=default_world_state(),
            runtime_provenance=replace(provenance, public_id="DL-2M2K-P9XR-W4TZ-C6HN"),
        )
        callback = RecordingTraceCallback()
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, callbacks=(callback,))
        service.start()
        self.registry.register(self.session)
        try:
            self.assertTrue(service.submit_manual("session-1").accepted)
            self.assertTrue(agent.started.wait(timeout=1))
            self.registry.unregister("session-1", session=self.session)
            self.registry.register(replacement)
            agent.release.set()
            self.assertTrue(service.wait_until_idle())
            self.assertEqual(
                callback.starts[0][2]["metadata"]["dreamlandPublicId"],
                provenance.public_id,
            )
            self.assertIs(agent.contexts[0].runtime_provenance, provenance)
            self.assertNotIn(
                "ambient:process-supervisor",
                replacement.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"],
            )
            self.assertNotEqual(service.list_jobs(1)[0].status, "applied")
        finally:
            agent.release.set()
            service.stop()

    def test_parent_trace_closes_after_completed_job_history_eviction(self):
        callback = RecordingTraceCallback()
        service = self._service(callbacks=(callback,))
        service.start()
        self.registry.register(self.session)

        def evict_completed_job(event) -> None:
            if event.event_type is not SessionEventType.STATE_MATERIALIZED:
                return
            job_id = event.payload.get("job_id")
            if not isinstance(job_id, str):
                return
            with service._lock:
                service._jobs.pop(job_id, None)

        subscription = self.event_bus.subscribe(
            "test-completed-job-eviction",
            evict_completed_job,
        )
        try:
            service.submit_manual("session-1")
            self.assertTrue(service.wait_until_idle())
            self.assertTrue(callback.ended.wait(timeout=1))

            self.assertEqual(len(callback.ends), 1)
            self.assertEqual(callback.ends[0][0]["application"]["status"], "applied")
        finally:
            subscription.close()
            service.stop()

    def test_parent_trace_records_shadow_and_graph_no_op(self):
        cases = (
            (
                "shadow",
                ProposalAgent(),
                "patch_proposed",
                {
                    "mode": "shadow",
                    "status": "shadow",
                    "reason": "shadow_mode",
                    "changed": False,
                },
            ),
            (
                "safe",
                NoOpAgent(),
                "no_op",
                {
                    "mode": "safe",
                    "status": "not_run",
                    "reason": "coordinator_no_op",
                    "changed": False,
                },
            ),
        )
        for apply_mode, agent, graph_status, application in cases:
            with self.subTest(apply_mode=apply_mode, graph_status=graph_status):
                event_bus = SessionEventBus()
                registry = LiveSessionRegistry(event_bus=event_bus)
                session = SessionState(
                    username="alice",
                    langfuse_trace_id=f"session-{apply_mode}-{graph_status}",
                    world_state=default_world_state(),
                )
                callback = RecordingTraceCallback()
                service = EnrichmentRuntimeService(
                    EnrichmentSettings(enabled=False, apply_mode=apply_mode),
                    session_registry=registry,
                    event_bus=event_bus,
                    agent=agent,
                    callbacks=(callback,),
                    max_workers=1,
                )
                service.start()
                registry.register(session)
                try:
                    service.submit_manual(session.langfuse_trace_id)
                    self.assertTrue(service.wait_until_idle())
                    self.assertTrue(callback.ended.wait(timeout=1))
                    outputs = callback.ends[0][0]
                    self.assertEqual(outputs["graph_status"], graph_status)
                    self.assertEqual(outputs["application"], application)
                finally:
                    service.stop()

    def test_parent_trace_closes_handled_runtime_failure(self):
        callback = RecordingTraceCallback()
        service = self._service(
            agent=FailingProposalAgent(),
            callbacks=(callback,),
        )
        service.start()
        self.registry.register(self.session)
        try:
            service.submit_manual("session-1")
            self.assertTrue(service.wait_until_idle())
            self.assertTrue(callback.ended.wait(timeout=1))

            self.assertEqual(callback.errors, [])
            outputs = callback.ends[0][0]
            self.assertIsNone(outputs["graph_status"])
            self.assertEqual(
                outputs["application"],
                {
                    "mode": "safe",
                    "status": "not_run",
                    "reason": "unhandled_runtime_error:RuntimeError",
                    "changed": False,
                },
            )
            self.assertEqual(service.list_jobs(1)[0].status, "failed")
        finally:
            service.stop()

    def test_command_arriving_before_enrichment_is_ready_is_logged_as_late(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        try:
            submission = service.submit_manual("session-1")
            self.assertTrue(submission.accepted)
            self.assertTrue(agent.started.wait(timeout=1))

            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_ACCEPTED,
                        source=EventSource.USER_COMMAND,
                        command_id="command-before-enrichment",
                        target_host="ubuntu",
                        surface="process",
                        payload={"command": "ps aux"},
                    )
                )

            self.assertIn("event=enrichment.late", "\n".join(captured.output))
        finally:
            agent.release.set()
            self.assertTrue(service.wait_until_idle())
            service.stop()

    def test_selected_surface_drives_ready_and_later_command_matching(self):
        service = self._service(
            agent=ProposalAgent(selected_surface="service"),
            apply_mode="safe",
        )
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as completed:
                submission = service.submit_manual(
                    "session-1",
                    surface="process",
                )
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertIsNotNone(submission.job_id)
            completion_output = "\n".join(completed.output)
            accepted_line = next(
                line for line in completed.output if "event=trigger.accepted" in line
            )
            self.assertIn(f"job_id={submission.job_id}", accepted_line)
            for event in ("job.completed", "enrichment.ready"):
                line = next(
                    line for line in completed.output if f"event={event}" in line
                )
                self.assertIn("requested_surface=process", line)
                self.assertIn("selected_surface=service", line)
                self.assertIn("surface=service", line)
            self.assertNotIn("surface=process status=applied", completion_output)

            with self.assertLogs("dreamland.enrichment", level="INFO") as matched:
                for command_id, surface in (
                    ("process-command", "process"),
                    ("service-command", "service"),
                ):
                    self.event_bus.publish(
                        new_session_event(
                            session_id="session-1",
                            event_type=SessionEventType.COMMAND_ACCEPTED,
                            source=EventSource.USER_COMMAND,
                            command_id=command_id,
                            target_host="ubuntu",
                            surface=surface,
                            payload={"command": "inspect"},
                        )
                    )

            match_lines = [
                line
                for line in matched.output
                if "event=enrichment.command_matched" in line
            ]
            self.assertEqual(len(match_lines), 1)
            self.assertIn("command_surface=service", match_lines[0])
            self.assertIn("requested_surface=process", match_lines[0])
            self.assertIn("selected_surface=service", match_lines[0])
            self.assertIn("surface=service", match_lines[0])
            self.assertIn("match_policy=interaction-focus-match-v1", match_lines[0])
            self.assertIn("match_classification=surface_only", match_lines[0])
        finally:
            service.stop()

    def test_command_match_classifies_exact_focus_without_logging_raw_path(self):
        service = self._service(
            agent=ProposalAgent(selected_surface="filesystem"),
            enabled=True,
            apply_mode="safe",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        focus = {
            "paths": ("/home/alice/reports",),
            "primary": {"kind": "path", "id": "/home/alice/reports"},
            "key": "path:/home/alice/reports",
        }
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={
                        "kind": "state_materialized",
                        "interaction_focus": focus,
                    },
                )
            )
            self.assertTrue(service.wait_until_idle())

            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_ACCEPTED,
                        source=EventSource.USER_COMMAND,
                        command_id="inspect-reports",
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={"interaction_focus": focus},
                    )
                )

            output = "\n".join(captured.output)
            self.assertIn("event=enrichment.command_matched", output)
            self.assertIn("match_classification=artifact_exact", output)
            self.assertIn("command_focus_path_count=1", output)
            self.assertNotIn("/home/alice/reports", output)
        finally:
            service.stop()

    def test_shadow_mode_proposes_without_mutating_session_state(self):
        service = self._service(enabled=False, apply_mode="shadow")
        service.start()
        self.registry.register(self.session)
        try:
            submission = service.submit_manual("session-1")
            self.assertTrue(submission.accepted)
            self.assertTrue(service.wait_until_idle())

            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
            self.assertEqual(service.list_jobs(1)[0].status, "shadow")
            self.assertEqual(service.list_jobs(1)[0].reason, "shadow_mode")
        finally:
            service.stop()

    def test_current_state_wins_if_it_changes_after_integrator_snapshot(self):
        def race_write() -> None:
            with registration.lock:
                facts = self.session.world_state["hosts_by_name"]["ubuntu"][
                    "facts_by_id"
                ]
                facts["ambient:process-supervisor"] = {
                    "description": "A user-visible command established another fact."
                }

        agent = ProposalAgent(after_current=race_write)
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            service.submit_manual("session-1")
            self.assertTrue(service.wait_until_idle())

            fact = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"][
                "ambient:process-supervisor"
            ]
            self.assertEqual(
                fact["description"],
                "A user-visible command established another fact.",
            )
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "touched_path_changed")
        finally:
            service.stop()

    def test_new_foreground_home_after_snapshot_protects_absent_authentication_path(
        self,
    ):
        def become_bob() -> None:
            with registration.lock:
                self.session.shell_stack = [
                    default_shell_frame(username="bob", hostname="ubuntu")
                ]

        agent = AuthenticationArtifactProposalAgent(after_current=become_bob)
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.assertTrue(service.submit_manual("session-1").accepted)
                self.assertTrue(service.wait_until_idle())
            host = self.session.world_state["hosts_by_name"]["ubuntu"]
            self.assertNotIn("ambient:process-supervisor", host["facts_by_id"])
            self.assertNotIn(
                "/home/bob/.ssh/id_ed25519_github",
                host.get("filesystem", {}).get("artifacts_by_path", {}),
            )
            self.assertEqual(
                service.list_jobs(1)[0].reason, "foreground_authentication_conflict"
            )
            self.assertIn(
                "foreground_authentication_conflict", "\n".join(captured.output)
            )
        finally:
            service.stop()

    def test_close_between_patch_validation_and_publication_prevents_mutation(self):
        service = self._service(apply_mode="safe")
        service.start()
        self.registry.register(self.session)

        def validate_then_close(*args, **kwargs):
            result = apply_enrichment_patch_pure(*args, **kwargs)
            self.registry.unregister("session-1", session=self.session)
            return result

        try:
            with patch(
                "dreamland.background_agents.enrichment.service.apply_enrichment_patch",
                side_effect=validate_then_close,
            ):
                submission = service.submit_manual("session-1")
                self.assertTrue(submission.accepted)
                self.assertTrue(service.wait_until_idle())
            self.assertNotIn(
                "ambient:process-supervisor",
                self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"],
            )
            self.assertEqual(
                service.list_jobs(1)[0].reason, "session_registration_changed"
            )
        finally:
            service.stop()

    def test_active_session_transition_skips_authority_bound_patch(self):
        def enter_program() -> None:
            self.session.interactive_program = InteractiveProgram(
                name="python",
                command="python3",
                argv=("python3",),
                prompt_template=">>> ",
            )

        agent = RefreshCapableAuthorityBoundProposalAgent(after_current=enter_program)
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="shell")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            self.assertEqual(agent.refresh_calls, 0)
            output = "\n".join(captured.output)
            self.assertIn("event=authority.semantic_conflict", output)
            self.assertIn("authority_domain=active_session", output)
            self.assertIn("action=cluster_skipped", output)
            self.assertIn("reason=active_session_conflict", output)
            self.assertNotIn("event=integrator.refresh.started", output)
        finally:
            service.stop()

    def test_foreground_evidence_change_skips_evidence_bound_patch(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        history.append(
            {
                "id": "date-1",
                "cwd": "/home/alice",
                "command": "date",
                "stdout": "Thu Nov 12 16:38:27 GST 2318\n",
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
            }
        )

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        service = self._service(
            agent=EvidenceBoundProposalAgent(after_current=add_direct_inventory),
            apply_mode="safe",
        )
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            output = "\n".join(captured.output)
            self.assertIn("authority_domain=foreground_evidence", output)
            self.assertIn("policy_version=foreground-evidence-v2", output)
        finally:
            service.stop()

    def test_foreground_evidence_conflict_refreshes_integrator_once(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        history.append(
            {
                "id": "date-1",
                "cwd": "/home/alice",
                "command": "date",
                "stdout": "Thu Nov 12 16:38:27 GST 2318\n",
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
            }
        )

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        lock_free_during_refresh: list[bool] = []

        def observe_refresh_lock() -> None:
            def try_lock() -> None:
                acquired = registration.lock.acquire(blocking=False)
                lock_free_during_refresh.append(acquired)
                if acquired:
                    registration.lock.release()

            probe = threading.Thread(target=try_lock)
            probe.start()
            probe.join(timeout=1)
            self.assertFalse(probe.is_alive())

        agent = RefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory,
            during_refresh=observe_refresh_lock,
        )
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertEqual(agent.refresh_calls, 1)
            self.assertEqual(lock_free_during_refresh, [True])
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertIn("ambient:process-supervisor", facts)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "applied")
            self.assertEqual(job.reason, "applied")
            output = "\n".join(captured.output)
            self.assertIn("event=integrator.refresh.decision", output)
            self.assertIn("eligible=true", output)
            self.assertIn("event=integrator.refresh.started", output)
            self.assertIn("event=integrator.refresh.patch_proposed", output)
            self.assertIn("event=integrator.refresh.completed", output)
            self.assertIn("integrator_refresh_outcome=applied", output)
            self.assertIn('conflict_domains=["foreground_evidence"]', output)
        finally:
            service.stop()

    def test_foreground_evidence_conflict_without_refresh_basis_stays_skipped(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        agent = MissingBasisRefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory,
        )
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertEqual(agent.refresh_calls, 0)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            output = "\n".join(captured.output)
            self.assertIn("reason=refresh_basis_unavailable", output)
            self.assertNotIn("event=integrator.refresh.started", output)
        finally:
            service.stop()

    def test_public_evidence_window_expiry_does_not_trigger_refresh(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        history.append(
            {
                "id": "ps-1",
                "cwd": "/home/alice",
                "command": "ps -ef",
                "stdout": (
                    "UID PID PPID C STIME TTY TIME CMD\n"
                    "root 1 0 0 16:00 ? 00:00:01 /sbin/init\n"
                ),
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
            }
        )

        def age_public_window() -> None:
            with registration.lock:
                for index in range(4):
                    history.append(
                        {
                            "id": f"pwd-{index}",
                            "cwd": "/home/alice",
                            "command": "pwd",
                            "stdout": "/home/alice\n",
                            "stderr": "",
                            "exit_status": 0,
                            "interrupted": False,
                            "signal": None,
                        }
                    )

        agent = RefreshingEvidenceBoundProposalAgent(after_current=age_public_window)
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="process")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertEqual(agent.refresh_calls, 0)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "applied")
            output = "\n".join(captured.output)
            self.assertNotIn("semantic_authority_conflict", output)
            self.assertNotIn("event=integrator.refresh.started", output)
        finally:
            service.stop()

    def test_second_foreground_evidence_conflict_is_terminal(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        history.append(
            {
                "id": "date-1",
                "cwd": "/home/alice",
                "command": "date",
                "stdout": "Thu Nov 12 16:38:27 GST 2318\n",
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
            }
        )

        def append_who(command_id: str, stdout: str) -> None:
            with registration.lock:
                history.append(
                    {
                        "id": command_id,
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": stdout,
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        agent = RefreshingEvidenceBoundProposalAgent(
            after_current=lambda: append_who(
                "who-1",
                "alice pts/0 2318-11-12 16:30\n",
            ),
            after_refresh_current=lambda: append_who(
                "who-2",
                "alice pts/0 2318-11-12 16:30\nbob pts/1 2318-11-12 16:40\n",
            ),
        )
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertEqual(agent.refresh_calls, 1)
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            output = "\n".join(captured.output)
            self.assertEqual(output.count("event=integrator.refresh.started"), 1)
            self.assertIn("event=integrator.refresh.completed", output)
            self.assertIn("reason=semantic_authority_conflict", output)
        finally:
            service.stop()

    def test_refreshed_patch_still_uses_normal_touched_path_safety(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        def foreground_wins_target_path() -> None:
            with registration.lock:
                facts = self.session.world_state["hosts_by_name"]["ubuntu"][
                    "facts_by_id"
                ]
                facts["ambient:process-supervisor"] = {
                    "description": "Foreground activity established this value."
                }

        agent = RefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory,
            after_refresh_current=foreground_wins_target_path,
        )
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        registration = self.registry.get("session-1")
        assert registration is not None
        try:
            submission = service.submit_manual("session-1", surface="identity")
            self.assertTrue(submission.accepted)
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(agent.refresh_calls, 1)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "touched_path_changed")
            fact = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"][
                "ambient:process-supervisor"
            ]
            self.assertEqual(
                fact["description"],
                "Foreground activity established this value.",
            )
        finally:
            service.stop()

    def test_pivot_before_refresh_snapshot_skips_without_integrator_result(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        self.session.world_state["hosts_by_name"]["cache"] = {
            "hostname": "cache",
            "facts_by_id": {},
        }

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        def pivot_before_refresh_capture() -> None:
            with registration.lock:
                self.session.world_state["active_host"] = "cache"

        agent = RefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory,
            during_refresh=pivot_before_refresh_capture,
        )
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertEqual(agent.refresh_calls, 1)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
            output = "\n".join(captured.output)
            self.assertIn("reason=target_not_foreground", output)
            self.assertIn("authority_domain=active_session", output)
            self.assertIn("action=refresh_skipped", output)
            self.assertNotIn("event=integrator.refresh.patch_proposed", output)
        finally:
            service.stop()

    def test_graph_caught_refresh_snapshot_pivot_is_terminal(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        self.session.world_state["hosts_by_name"]["cache"] = {
            "hostname": "cache",
            "facts_by_id": {},
        }

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        def pivot_before_refresh_capture() -> None:
            with registration.lock:
                self.session.world_state["active_host"] = "cache"

        agent = GraphCaughtPivotRefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory,
            during_refresh=pivot_before_refresh_capture,
        )
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertEqual(agent.refresh_calls, 1)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            output = "\n".join(captured.output)
            self.assertIn("event=authority.semantic_conflict", output)
            self.assertIn("authority_domain=active_session", output)
            self.assertIn("action=refresh_skipped", output)
            self.assertIn("reason=target_not_foreground", output)
            self.assertNotIn("event=integrator.refresh.patch_proposed", output)
        finally:
            service.stop()

    def test_pivot_during_refreshed_integrator_latency_skips_final_patch(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        self.session.world_state["hosts_by_name"]["cache"] = {
            "hostname": "cache",
            "facts_by_id": {},
        }

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        def pivot_after_refresh_capture() -> None:
            with registration.lock:
                self.session.world_state["active_host"] = "cache"

        agent = RefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory,
            after_refresh_current=pivot_after_refresh_capture,
        )
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            self.assertEqual(agent.refresh_calls, 1)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
            output = "\n".join(captured.output)
            self.assertIn("event=integrator.refresh.patch_proposed", output)
            self.assertIn("reason=target_not_foreground", output)
            self.assertIn("authority_domain=active_session", output)
            self.assertIn("authority_dependency_count=1", output)
        finally:
            service.stop()

    def test_refresh_non_patch_outcomes_finish_once_and_close_root_trace(self):
        cases = (
            (
                NoOpRefreshingEvidenceBoundProposalAgent,
                "no_op",
                "integrator_no_op",
            ),
            (
                RejectingRefreshingEvidenceBoundProposalAgent,
                "rejected",
                "integrator_output_invalid",
            ),
            (
                TerminalRefreshingEvidenceBoundProposalAgent,
                "rejected",
                "integrator_model_failed",
            ),
        )
        for agent_type, expected_status, expected_reason in cases:
            with self.subTest(agent=agent_type.__name__):
                event_bus = SessionEventBus()
                registry = LiveSessionRegistry(event_bus=event_bus)
                session = SessionState(
                    username="alice",
                    langfuse_trace_id=f"session-{agent_type.__name__}",
                    world_state=default_world_state(),
                )
                history = session.world_state["hosts_by_name"]["ubuntu"].setdefault(
                    "command_history",
                    [],
                )
                registration_holder = {}

                def add_direct_inventory(
                    history=history,
                    registration_holder=registration_holder,
                ) -> None:
                    registration = registration_holder["registration"]
                    with registration.lock:
                        history.append(
                            {
                                "id": "who-1",
                                "cwd": "/home/alice",
                                "command": "who",
                                "stdout": "alice pts/0 2318-11-12 16:30\n",
                                "stderr": "",
                                "exit_status": 0,
                                "interrupted": False,
                                "signal": None,
                            }
                        )

                agent = agent_type(after_current=add_direct_inventory)
                callback = RecordingTraceCallback()
                service = EnrichmentRuntimeService(
                    EnrichmentSettings(enabled=False, apply_mode="safe"),
                    session_registry=registry,
                    event_bus=event_bus,
                    agent=agent,
                    callbacks=(callback,),
                    max_workers=1,
                )
                service.start()
                registration_holder["registration"] = registry.register(session)
                try:
                    with self.assertLogs(
                        "dreamland.enrichment",
                        level="INFO",
                    ) as captured:
                        submission = service.submit_manual(
                            session.langfuse_trace_id,
                            surface="identity",
                        )
                        self.assertTrue(submission.accepted)
                        self.assertTrue(service.wait_until_idle())
                        self.assertTrue(callback.ended.wait(timeout=1))

                    self.assertEqual(agent.refresh_calls, 1)
                    job = service.list_jobs(1)[0]
                    self.assertEqual(job.status, expected_status)
                    self.assertEqual(job.reason, expected_reason)
                    self.assertEqual(len(callback.ends), 1)
                    outputs = callback.ends[0][0]
                    self.assertEqual(outputs["graph_status"], "patch_proposed")
                    self.assertEqual(
                        outputs["application"],
                        {
                            "mode": "safe",
                            "status": expected_status,
                            "reason": expected_reason,
                            "changed": False,
                        },
                    )
                    facts = session.world_state["hosts_by_name"]["ubuntu"][
                        "facts_by_id"
                    ]
                    self.assertNotIn("ambient:process-supervisor", facts)
                    output = "\n".join(captured.output)
                    if agent_type is TerminalRefreshingEvidenceBoundProposalAgent:
                        self.assertIn("event=provider_circuit.opened", output)
                        self.assertIn("role=integrator", output)
                        self.assertIn("failure_disposition=terminal", output)
                    else:
                        self.assertNotIn("event=provider_circuit.opened", output)
                finally:
                    service.stop()

    def test_runtime_stop_discards_non_patch_integrator_refresh_result(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        agent = BlockingNoOpRefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory
        )
        callback = RecordingTraceCallback()
        service = self._service(
            agent=agent,
            apply_mode="safe",
            callbacks=(callback,),
        )
        service.start()
        registration = self.registry.register(self.session)
        try:
            submission = service.submit_manual("session-1", surface="identity")
            self.assertTrue(submission.accepted)
            self.assertTrue(agent.refresh_started.wait(timeout=1))

            service.stop(wait=False)
            agent.refresh_release.set()
            self.assertTrue(_wait_for_job_status(service, submission.job_id, "skipped"))
            self.assertTrue(callback.ended.wait(timeout=1))

            job = service.list_jobs(1)[0]
            self.assertEqual(job.reason, "runtime_stopped")
            self.assertEqual(len(callback.ends), 1)
            self.assertEqual(
                callback.ends[0][0]["application"],
                {
                    "mode": "safe",
                    "status": "skipped",
                    "reason": "runtime_stopped",
                    "changed": False,
                },
            )
        finally:
            agent.refresh_release.set()
            service.stop()

    def test_registration_change_discards_non_patch_integrator_refresh_result(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )

        def add_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        agent = BlockingNoOpRefreshingEvidenceBoundProposalAgent(
            after_current=add_direct_inventory
        )
        callback = RecordingTraceCallback()
        service = self._service(
            agent=agent,
            apply_mode="safe",
            callbacks=(callback,),
        )
        service.start()
        registration = self.registry.register(self.session)
        replacement = SessionState(
            username="bob",
            langfuse_trace_id="session-1",
            world_state=default_world_state(),
        )
        try:
            submission = service.submit_manual("session-1", surface="identity")
            self.assertTrue(submission.accepted)
            self.assertTrue(agent.refresh_started.wait(timeout=1))

            self.registry.unregister("session-1", session=self.session)
            self.registry.register(replacement)
            agent.refresh_release.set()
            self.assertTrue(_wait_for_job_status(service, submission.job_id, "skipped"))
            self.assertTrue(callback.ended.wait(timeout=1))

            job = service.list_jobs(1)[0]
            self.assertEqual(job.reason, "session_registration_changed")
            self.assertEqual(len(callback.ends), 1)
            self.assertEqual(
                callback.ends[0][0]["application"],
                {
                    "mode": "safe",
                    "status": "skipped",
                    "reason": "session_registration_changed",
                    "changed": False,
                },
            )
        finally:
            agent.refresh_release.set()
            service.stop()

    def test_first_foreground_evidence_skips_empty_evidence_bound_patch(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )

        def add_first_direct_inventory() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "who-1",
                        "cwd": "/home/alice",
                        "command": "who",
                        "stdout": "alice pts/0 2318-11-12 16:30\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        service = self._service(
            agent=EvidenceBoundProposalAgent(after_current=add_first_direct_inventory),
            apply_mode="safe",
        )
        service.start()
        registration = self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual("session-1", surface="identity")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "semantic_authority_conflict")
            output = "\n".join(captured.output)
            self.assertIn("authority_domain=foreground_evidence", output)
        finally:
            service.stop()

    def test_unrelated_command_does_not_invalidate_foreground_evidence(self):
        history = self.session.world_state["hosts_by_name"]["ubuntu"].setdefault(
            "command_history",
            [],
        )
        history.append(
            {
                "id": "date-1",
                "cwd": "/home/alice",
                "command": "date",
                "stdout": "Thu Nov 12 16:38:27 GST 2318\n",
                "stderr": "",
                "exit_status": 0,
                "interrupted": False,
                "signal": None,
            }
        )

        def add_unrelated_command() -> None:
            with registration.lock:
                history.append(
                    {
                        "id": "pwd-1",
                        "cwd": "/home/alice",
                        "command": "pwd",
                        "stdout": "/home/alice\n",
                        "stderr": "",
                        "exit_status": 0,
                        "interrupted": False,
                        "signal": None,
                    }
                )

        service = self._service(
            agent=EvidenceBoundProposalAgent(after_current=add_unrelated_command),
            apply_mode="safe",
        )
        service.start()
        registration = self.registry.register(self.session)
        try:
            submission = service.submit_manual("session-1", surface="filesystem")
            self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertIn("ambient:process-supervisor", facts)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "applied")
        finally:
            service.stop()

    def test_automatic_state_trigger_is_rate_limited_with_explanation(self):
        service = self._service(
            enabled=True,
            apply_mode="shadow",
            max_jobs_per_minute=1,
        )
        service.start()
        self.registry.register(self.session)
        try:
            first = new_session_event(
                session_id="session-1",
                event_type=SessionEventType.STATE_MATERIALIZED,
                source=EventSource.COMMAND_EMULATOR,
                target_host="ubuntu",
                surface="process",
                payload={"kind": "state_materialized"},
            )
            self.event_bus.publish(first)
            self.assertTrue(service.wait_until_idle())
            second = new_session_event(
                session_id="session-1",
                event_type=SessionEventType.STATE_MATERIALIZED,
                source=EventSource.COMMAND_EMULATOR,
                target_host="ubuntu",
                surface="network",
                payload={"kind": "state_materialized"},
            )
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(second)

            self.assertEqual(len(service.list_jobs(10)), 1)
            output = "\n".join(captured.output)
            self.assertIn("reason=rate_limit_reached", output)
            self.assertIn("observed=1", output)
            self.assertIn("threshold=1", output)
            self.assertIn("retry_after_ms=", output)
        finally:
            service.stop()

    def test_terminal_graph_failure_suppresses_only_later_automatic_jobs(self):
        agent = ClassifiedFailureAgent(terminal=True)
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            event = new_session_event(
                session_id="session-1",
                event_type=SessionEventType.STATE_MATERIALIZED,
                source=EventSource.COMMAND_EMULATOR,
                target_host="ubuntu",
                surface="process",
                payload={"kind": "state_materialized"},
            )
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(event)
                self.assertTrue(service.wait_until_idle())
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        target_host="ubuntu",
                        surface="network",
                        payload={"kind": "state_materialized"},
                    )
                )
                manual = service.submit_manual("session-1", surface="service")
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(manual.accepted)
            self.assertEqual(agent.calls, 2)
            self.assertEqual(len(service.list_jobs(10)), 2)
            output = "\n".join(captured.output)
            self.assertIn("event=provider_circuit.opened", output)
            self.assertIn("failure_kind=provider_credit_exhausted", output)
            self.assertIn("failure_disposition=terminal", output)
            self.assertIn("reason=provider_circuit_open", output)
            self.assertIn("stage=trigger_admission", output)
        finally:
            service.stop()

    def test_transient_graph_failure_does_not_open_provider_circuit(self):
        agent = ClassifiedFailureAgent(terminal=False)
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                for surface in ("process", "network"):
                    self.event_bus.publish(
                        new_session_event(
                            session_id="session-1",
                            event_type=SessionEventType.STATE_MATERIALIZED,
                            source=EventSource.COMMAND_EMULATOR,
                            target_host="ubuntu",
                            surface=surface,
                            payload={"kind": "state_materialized"},
                        )
                    )
                    self.assertTrue(service.wait_until_idle())

            self.assertEqual(agent.calls, 2)
            self.assertNotIn(
                "event=provider_circuit.opened",
                "\n".join(captured.output),
            )
        finally:
            service.stop()

    def test_service_restart_resets_terminal_provider_circuit(self):
        agent = ClassifiedFailureAgent(terminal=True)
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="process",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(service.wait_until_idle())
            service.stop()

            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                service.start()
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        target_host="ubuntu",
                        surface="network",
                        payload={"kind": "state_materialized"},
                    )
                )
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(agent.calls, 2)
            self.assertIn("event=provider_circuit.reset", "\n".join(captured.output))
        finally:
            service.stop()

    def test_foreground_terminal_failure_prevents_redundant_automatic_call(self):
        agent = ProviderAwareProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        command_id="failed-command",
                        target_host="ubuntu",
                        surface="process",
                        payload={
                            "exit_status": 75,
                            "provider_failure": {
                                "kind": "provider_credit_exhausted",
                                "disposition": "terminal",
                                "provider": "openai",
                                "model": "gpt-5.6-terra",
                                "code": "credit_balance_exhausted",
                                "status": 429,
                            },
                        },
                    )
                )
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        target_host="ubuntu",
                        surface="process",
                        payload={"kind": "state_materialized"},
                    )
                )

            self.assertEqual(agent.contexts, [])
            self.assertEqual(service.list_jobs(10), ())
            output = "\n".join(captured.output)
            self.assertIn("circuit_origin=foreground_command", output)
            self.assertIn("reason=provider_circuit_open", output)
        finally:
            service.stop()

    def test_non_emulator_failure_notice_cannot_open_provider_circuit(self):
        agent = ProviderAwareProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.USER_COMMAND,
                        target_host="ubuntu",
                        surface="process",
                        payload={
                            "provider_failure": {
                                "kind": "provider_credit_exhausted",
                                "disposition": "terminal",
                                "provider": "openai",
                            }
                        },
                    )
                )
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        target_host="ubuntu",
                        surface="process",
                        payload={"kind": "state_materialized"},
                    )
                )
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 1)
            self.assertIn("reason=untrusted_event_source", "\n".join(captured.output))
        finally:
            service.stop()

    def test_terminal_circuit_cancels_idle_and_deferred_automatic_work(self):
        agent = BlockingFirstProposalAgent()
        agent.provider_names = frozenset({"openai"})
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized", "session_idle"}),
            idle_delay_ms=5_000,
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        target_host="ubuntu",
                        surface="network",
                        payload={"kind": "state_materialized"},
                    )
                )
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={"exit_status": 0},
                    )
                )
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={
                            "exit_status": 75,
                            "provider_failure": {
                                "kind": "provider_authentication",
                                "disposition": "terminal",
                                "provider": "openai",
                                "status": 401,
                            },
                        },
                    )
                )
                agent.release.set()
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(service.list_jobs(10)), 1)
            self.assertEqual(service._deferred_by_session, {})
            self.assertEqual(service._idle_timers, {})
            output = "\n".join(captured.output)
            self.assertIn("event=deferred.cancelled", output)
            self.assertIn("event=idle.cancelled", output)
            self.assertIn("reason=provider_circuit_open", output)
        finally:
            agent.release.set()
            service.stop()

    def test_self_generated_state_event_does_not_retrigger_agent(self):
        service = self._service(enabled=True, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        try:
            event = new_session_event(
                session_id="session-1",
                event_type=SessionEventType.STATE_MATERIALIZED,
                source=EventSource.COMMAND_EMULATOR,
                target_host="ubuntu",
                surface="process",
                payload={"kind": "state_materialized"},
            )
            self.event_bus.publish(event)
            self.assertTrue(service.wait_until_idle())
            self.assertEqual(len(service.list_jobs(10)), 1)
        finally:
            service.stop()

    def test_self_generated_state_event_suppresses_ambient_followthrough(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="safe",
            triggers=frozenset({"materialization_followthrough"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.BACKGROUND_AGENT,
                    source_agent_id="enrichment",
                    target_host="ubuntu",
                    surface="process",
                    payload={"kind": "enrichment_applied"},
                )
            )
            self.assertTrue(service.wait_until_idle())
            self.assertEqual(agent.contexts, [])
            self.assertEqual(service.list_jobs(10), ())
        finally:
            service.stop()

    def test_same_correlation_can_launch_one_focused_and_one_ambient_job(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized", "materialization_followthrough"}),
            max_inflight_per_session=2,
            max_workers=2,
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id="command-materialized",
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 2)
            self.assertEqual(
                {context.mode for context in agent.contexts},
                {EnrichmentMode.FOCUSED, EnrichmentMode.AMBIENT_PRESENCE},
            )
            self.assertEqual(
                {context.trigger_id for context in agent.contexts},
                {"state_materialized", "materialization_followthrough"},
            )
        finally:
            service.stop()

    def test_distinct_materialized_subjects_have_distinct_cooldown_scopes(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
            cooldown_ms=60_000,
        )
        service.start()
        self.registry.register(self.session)
        try:
            for index, path in enumerate(("/srv/catalog/a", "/srv/catalog/b")):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id=f"command-{index}",
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={
                            "kind": "state_materialized",
                            "interaction_focus": {
                                "paths": (path,),
                                "primary": {"kind": "path", "id": path},
                                "key": f"path:{path}",
                            },
                        },
                    )
                )
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 2)
            self.assertEqual(
                [context.interaction_focus["key"] for context in agent.contexts],
                ["path:/srv/catalog/a", "path:/srv/catalog/b"],
            )
        finally:
            service.stop()

    def test_registration_bootstraps_ambient_presence_once_per_host(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"host_presence_bootstrap"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.assertTrue(service.wait_until_idle())
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.ACTIVE_HOST_CHANGED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="host",
                )
            )
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 1)
            self.assertEqual(
                agent.contexts[0].mode,
                EnrichmentMode.AMBIENT_PRESENCE,
            )
            self.assertEqual(
                agent.contexts[0].trigger_id,
                "host_presence_bootstrap",
            )
        finally:
            service.stop()

    def test_active_host_bootstrap_eventually_runs_after_deferred_replacement(self):
        self.session.world_state["hosts_by_name"]["cache"] = {
            "hostname": "cache",
            "facts_by_id": {},
        }
        agent = BlockingManyProposalAgent(3)
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"host_presence_bootstrap", "state_materialized"}),
            max_jobs_per_minute=10,
        )
        self.registry.register(self.session)
        service.start()
        try:
            submission = service.submit_manual("session-1")
            self.assertTrue(submission.accepted)
            self.assertTrue(agent.started[0].wait(timeout=1))

            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.ACTIVE_HOST_CHANGED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id="active-cache",
                        target_host="cache",
                        surface="host",
                    )
                )
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id="focused-work",
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={"kind": "state_materialized"},
                    )
                )

                agent.release[0].set()
                self.assertTrue(agent.started[1].wait(timeout=1))
                agent.release[1].set()
                self.assertTrue(agent.started[2].wait(timeout=1))
                agent.release[2].set()
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(
                [context.trigger_id for context in agent.contexts],
                ["manual", "state_materialized", "host_presence_bootstrap"],
            )
            self.assertEqual(agent.contexts[-1].target_host, "cache")
            self.assertEqual(service._active_bootstrap_retries, {})
            output = "\n".join(captured.output)
            self.assertIn("event=bootstrap.retry_stored", output)
            self.assertIn("event=bootstrap.retry_retained", output)
            self.assertIn("event=bootstrap.retry_admitted", output)
            self.assertIn("action=job_completed", output)
        finally:
            for release in agent.release:
                release.set()
            service.stop()

    def test_active_host_bootstrap_wins_equal_priority_deferred_slot(self):
        for hostname in ("cache-a", "cache-b", "cache-c"):
            self.session.world_state["hosts_by_name"][hostname] = {
                "hostname": hostname,
                "facts_by_id": {},
            }
        agent = BlockingManyProposalAgent(2)
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"host_presence_bootstrap"}),
            max_jobs_per_minute=10,
        )
        self.registry.register(self.session)
        service.start()
        try:
            submission = service.submit_manual("session-1")
            self.assertTrue(submission.accepted)
            self.assertTrue(agent.started[0].wait(timeout=1))

            def publish_discovery(hostname: str) -> None:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id=f"discovered-{hostname}",
                        target_host="ubuntu",
                        surface="network",
                        anchors=(hostname,),
                        payload={"kind": "host_discovered"},
                    )
                )

            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                publish_discovery("cache-a")
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.ACTIVE_HOST_CHANGED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id="active-cache-b",
                        target_host="cache-b",
                        surface="host",
                    )
                )
                publish_discovery("cache-c")

                deferred = service._deferred_by_session["session-1"].candidate
                self.assertEqual(deferred.target_host, "cache-b")
                self.assertIs(
                    deferred.details.get("active_host_bootstrap"),
                    True,
                )

                agent.release[0].set()
                self.assertTrue(agent.started[1].wait(timeout=1))
                agent.release[1].set()
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(
                [context.trigger_id for context in agent.contexts],
                ["manual", "host_presence_bootstrap"],
            )
            self.assertEqual(agent.contexts[-1].target_host, "cache-b")
            self.assertEqual(service._active_bootstrap_retries, {})
            preference_records = [
                record
                for record in captured.records
                if record.dreamland_enrichment_fields.get("reason")
                == "active_host_preferred"
            ]
            self.assertEqual(
                [record.dreamland_enrichment_event for record in preference_records],
                ["deferred.replaced", "deferred.retained"],
            )
        finally:
            for release in agent.release:
                release.set()
            service.stop()

    def test_successful_presence_probe_triggers_ambient_job(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"presence_probe"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            for command_id, exit_status in (("failed-ps", 1), ("successful-ps", 0)):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id=command_id,
                        command_id=command_id,
                        target_host="ubuntu",
                        surface="process",
                        payload={
                            "exit_status": exit_status,
                            "interrupted": False,
                            "presence_probe": "process_activity",
                        },
                    )
                )
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 1)
            context = agent.contexts[0]
            self.assertEqual(context.trigger_id, "presence_probe")
            self.assertEqual(context.mode, EnrichmentMode.AMBIENT_PRESENCE)
        finally:
            service.stop()

    def test_subject_first_touch_is_focused_and_fires_once(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"subject_first_touch"}),
        )
        service.start()
        self.registry.register(self.session)
        focus = {
            "paths": ("/srv/catalog/config.yaml",),
            "primary": {"kind": "path", "id": "/srv/catalog/config.yaml"},
            "key": "path:/srv/catalog/config.yaml",
        }
        try:
            for command_id in ("first", "again"):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id=command_id,
                        command_id=command_id,
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={
                            "exit_status": 0,
                            "interrupted": False,
                            "interaction_focus": focus,
                        },
                    )
                )
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 1)
            self.assertEqual(agent.contexts[0].trigger_id, "subject_first_touch")
            self.assertEqual(agent.contexts[0].mode, EnrichmentMode.FOCUSED)
        finally:
            service.stop()

    def test_interaction_pulse_runs_after_every_four_successful_commands(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"interaction_pulse"}),
            max_jobs_per_minute=10,
        )
        service.start()
        self.registry.register(self.session)
        try:
            with (
                patch(
                    "dreamland.background_agents.enrichment.service._monotonic_ms",
                    return_value=100_000,
                ),
                self.assertLogs("dreamland.enrichment", level="INFO") as captured,
            ):
                for index in range(8):
                    self.event_bus.publish(
                        new_session_event(
                            session_id="session-1",
                            event_type=SessionEventType.COMMAND_COMPLETED,
                            source=EventSource.COMMAND_EMULATOR,
                            correlation_id=f"command-{index}",
                            command_id=f"command-{index}",
                            target_host="ubuntu",
                            surface="shell",
                            payload={"exit_status": 0, "interrupted": False},
                        )
                    )
                    self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 2)
            self.assertTrue(
                all(
                    context.mode is EnrichmentMode.AMBIENT_PRESENCE
                    for context in agent.contexts
                )
            )
            reset_records = [
                record
                for record in captured.records
                if record.dreamland_enrichment_event
                == "interaction_pulse.counter_reset"
            ]
            self.assertEqual(len(reset_records), 2)
            self.assertEqual(
                [
                    record.dreamland_enrichment_fields["pulse_count"]
                    for record in reset_records
                ],
                [4, 4],
            )
            self.assertTrue(
                all(
                    record.dreamland_enrichment_fields["reason"]
                    == "interaction_pulse_accepted"
                    for record in reset_records
                )
            )
        finally:
            service.stop()

    def test_other_ambient_acceptance_does_not_reset_interaction_count(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"interaction_pulse", "session_idle"}),
            idle_delay_ms=60_000,
            max_jobs_per_minute=10,
        )
        service.start()
        self.registry.register(self.session)

        def publish_command(index: int) -> None:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.COMMAND_COMPLETED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id=f"command-{index}",
                    command_id=f"command-{index}",
                    target_host="ubuntu",
                    surface="shell",
                    payload={"exit_status": 0, "interrupted": False},
                )
            )

        try:
            publish_command(0)
            publish_command(1)
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.SESSION_IDLE,
                    source=EventSource.SESSION_RUNTIME,
                    correlation_id="manual-idle",
                    target_host="ubuntu",
                    surface="shell",
                )
            )
            self.assertTrue(service.wait_until_idle())
            publish_command(2)
            publish_command(3)
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(
                [context.trigger_id for context in agent.contexts],
                ["session_idle", "interaction_pulse"],
            )
        finally:
            service.stop()

    def test_interaction_pulse_suppresses_correlated_idle_candidate(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"interaction_pulse", "session_idle"}),
            idle_delay_ms=60_000,
            max_jobs_per_minute=10,
        )
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                for index in range(4):
                    command_id = f"command-{index}"
                    self.event_bus.publish(
                        new_session_event(
                            session_id="session-1",
                            event_type=SessionEventType.COMMAND_COMPLETED,
                            source=EventSource.COMMAND_EMULATOR,
                            correlation_id=command_id,
                            command_id=command_id,
                            target_host="ubuntu",
                            surface="shell",
                            payload={"exit_status": 0, "interrupted": False},
                        )
                    )
                self.assertTrue(service.wait_until_idle())

                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.SESSION_IDLE,
                        source=EventSource.SESSION_RUNTIME,
                        correlation_id="command-3",
                        caused_by_event_id="command-completed-event",
                        command_id="command-3",
                        target_host="ubuntu",
                        surface="shell",
                    )
                )
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 1)
            self.assertEqual(agent.contexts[0].trigger_id, "interaction_pulse")
            self.assertEqual(
                agent.contexts[0].mode,
                EnrichmentMode.AMBIENT_PRESENCE,
            )
            output = "\n".join(captured.output)
            self.assertIn("event=idle.cancelled", output)
            self.assertIn("reason=interaction_pulse_candidate", output)
            self.assertIn("reason=higher_priority_correlated_candidate", output)
            self.assertIn("higher_priority_trigger=interaction_pulse", output)
            self.assertEqual(len(service.list_jobs(10)), 1)
        finally:
            service.stop()

    def test_three_jobs_run_concurrently_and_fourth_uses_deferred_slot(self):
        agent = BlockingManyProposalAgent(4)
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
            max_jobs_per_minute=10,
            max_inflight_per_session=3,
            max_workers=4,
        )
        service.start()
        self.registry.register(self.session)
        try:

            def publish(index: int) -> None:
                path = f"/srv/application-{index}"
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id=f"command-{index}",
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={
                            "kind": "state_materialized",
                            "interaction_focus": {
                                "paths": (path,),
                                "primary": {"kind": "path", "id": path},
                                "key": f"path:{path}",
                            },
                        },
                    )
                )

            for index in range(3):
                publish(index)
            self.assertTrue(
                all(started.wait(timeout=1) for started in agent.started[:3])
            )

            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                publish(3)
                self.assertEqual(len(service._deferred_by_session), 1)
                self.assertFalse(agent.started[3].is_set())
                agent.release[0].set()
                self.assertTrue(agent.started[3].wait(timeout=1))
                for release in agent.release[1:]:
                    release.set()
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(agent.max_active, 3)
            self.assertEqual(len(service.list_jobs(10)), 4)
            self.assertEqual(service._deferred_by_session, {})
            output = "\n".join(captured.output)
            self.assertIn("event=deferred.stored", output)
            self.assertIn("event=deferred.released", output)
            self.assertIn("event=deferred.scheduled", output)
            inflight_record = next(
                record
                for record in captured.records
                if record.dreamland_enrichment_event == "trigger.suppressed"
                and record.dreamland_enrichment_fields.get("reason")
                == "inflight_limit_reached"
            )
            self.assertEqual(
                inflight_record.dreamland_enrichment_fields["enrichment_mode"],
                "focused",
            )
            self.assertEqual(
                inflight_record.dreamland_enrichment_fields["inflight_before"],
                3,
            )
            self.assertEqual(
                inflight_record.dreamland_enrichment_fields["mode_inflight_before"],
                3,
            )
            self.assertEqual(
                inflight_record.dreamland_enrichment_fields["max_inflight"],
                3,
            )
        finally:
            for release in agent.release:
                release.set()
            service.stop()

    def test_repeated_surface_focus_triggers_on_the_second_matching_command(self):
        service = self._service(
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"surface_focus"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            for command_id in ("command-1", "command-2"):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        command_id=command_id,
                        target_host="ubuntu",
                        surface="process",
                        payload={"exit_status": 0},
                    )
                )
            self.assertTrue(service.wait_until_idle())

            jobs = service.list_jobs(10)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].trigger, "surface_focus")
            self.assertEqual(jobs[0].surface, "process")
        finally:
            service.stop()

    def test_idle_trigger_runs_after_quiet_delay(self):
        service = self._service(
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"session_idle"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.COMMAND_COMPLETED,
                    source=EventSource.COMMAND_EMULATOR,
                    command_id="command-idle",
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"exit_status": 0},
                )
            )

            self.assertTrue(_wait_for_job_count(service, 1))
            self.assertTrue(service.wait_until_idle())
            self.assertEqual(service.list_jobs(1)[0].trigger, "session_idle")
        finally:
            service.stop()

    def test_host_discovery_trigger_reads_ahead_into_the_new_host(self):
        self.session.world_state["hosts_by_name"]["cache"] = {
            "hostname": "cache",
            "facts_by_id": {},
            "services_by_name": {"redis": {"state": "running"}},
        }
        service = self._service(
            enabled=True,
            apply_mode="safe",
            triggers=frozenset({"host_discovered"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="host",
                    anchors=("cache",),
                    payload={"kind": "host_discovered"},
                )
            )
            self.assertTrue(service.wait_until_idle())

            job = service.list_jobs(1)[0]
            self.assertEqual(job.target_host, "cache")
            facts = self.session.world_state["hosts_by_name"]["cache"]["facts_by_id"]
            self.assertIn("ambient:process-supervisor", facts)
        finally:
            service.stop()

    def test_host_discovery_outranks_same_command_surface_focus(self):
        proposal_agent = ProposalAgent()
        service = self._service(
            agent=proposal_agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"host_discovered", "surface_focus"}),
        )
        backend = AgentBackend(
            server_profile(),
            agent=DiscoverOnSecondNetworkCommandAgent(),
            event_bus=self.event_bus,
        )
        session = backend.create_session("alice")
        service.start()
        self.registry.register(session)
        try:
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                backend.execute(session, "nmap 10.0.0.0/24")
                backend.execute(session, "nmap 10.0.0.0/24")
                self.assertTrue(service.wait_until_idle())

            jobs = service.list_jobs(10)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].trigger, "host_discovered")
            self.assertEqual(jobs[0].target_host, "cache")
            output = "\n".join(captured.output)
            self.assertIn("reason=higher_priority_correlated_candidate", output)
            self.assertIn("higher_priority_trigger=host_discovered", output)
        finally:
            service.stop()

    def test_active_host_change_outranks_correlated_host_discovery(self):
        service = self._service(
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset(
                {"active_host_changed", "host_discovered", "surface_focus"}
            ),
        )
        backend = AgentBackend(
            server_profile(),
            agent=PivotAndDiscoverAgent(),
            event_bus=self.event_bus,
        )
        session = backend.create_session("alice")
        service.start()
        self.registry.register(session)
        try:
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                backend.execute(session, "ssh cache")
                self.assertTrue(service.wait_until_idle())

            jobs = service.list_jobs(10)
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].trigger, "active_host_changed")
            self.assertEqual(jobs[0].target_host, "cache")
            output = "\n".join(captured.output)
            self.assertIn("reason=higher_priority_correlated_candidate", output)
            self.assertIn("higher_priority_trigger=active_host_changed", output)
        finally:
            service.stop()

    def test_deferred_candidate_is_replaced_by_higher_priority_work(self):
        self.session.world_state["hosts_by_name"]["cache"] = {
            "hostname": "cache",
            "facts_by_id": {},
        }
        agent = BlockingFirstProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset(
                {"state_materialized", "session_idle", "host_discovered"}
            ),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id="command-running",
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))

            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.SESSION_IDLE,
                        source=EventSource.SESSION_RUNTIME,
                        correlation_id="command-idle",
                        target_host="ubuntu",
                        surface="filesystem",
                    )
                )
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id="command-discovery",
                        target_host="ubuntu",
                        surface="network",
                        anchors=("cache",),
                        payload={"kind": "host_discovered"},
                    )
                )
                agent.release.set()
                self.assertTrue(service.wait_until_idle())

            jobs = service.list_jobs(10)
            self.assertEqual(len(jobs), 2)
            self.assertEqual(
                {job.trigger for job in jobs},
                {"state_materialized", "host_discovered"},
            )
            self.assertEqual(agent.contexts[-1].target_host, "cache")
            output = "\n".join(captured.output)
            self.assertIn("event=deferred.stored", output)
            self.assertIn("event=deferred.replaced", output)
            self.assertIn("retained_trigger=host_discovered", output)
            self.assertIn("event=deferred.released", output)
            self.assertIn("event=deferred.scheduled", output)
            scheduled_record = next(
                record
                for record in captured.records
                if record.dreamland_enrichment_event == "deferred.scheduled"
            )
            self.assertEqual(
                scheduled_record.dreamland_enrichment_job_id,
                jobs[0].job_id,
            )
            self.assertEqual(
                scheduled_record.dreamland_enrichment_fields["completed_job_id"],
                jobs[1].job_id,
            )
        finally:
            agent.release.set()
            service.stop()

    def test_newer_equal_priority_deferred_focus_replaces_older_focus(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id="command-running",
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))
            for correlation_id, path in (
                ("command-old", "/home/alice/old"),
                ("command-new", "/home/alice/new"),
            ):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id=correlation_id,
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={
                            "kind": "state_materialized",
                            "interaction_focus": {
                                "cwd": "/home/alice",
                                "paths": (path,),
                                "primary": {"kind": "path", "id": path},
                                "key": f"path:{path}",
                            },
                        },
                    )
                )

            agent.release.set()
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(service.list_jobs(10)), 2)
            self.assertEqual(
                agent.contexts[-1].interaction_focus["paths"],
                ["/home/alice/new"],
            )
            self.assertEqual(
                agent.contexts[-1].interaction_focus["key"],
                "path:/home/alice/new",
            )
        finally:
            agent.release.set()
            service.stop()

    def test_same_correlation_lower_priority_signal_is_terminally_suppressed(self):
        self.session.world_state["hosts_by_name"]["cache"] = {
            "hostname": "cache",
            "facts_by_id": {},
        }
        agent = BlockingFirstProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"active_host_changed", "host_discovered"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.ACTIVE_HOST_CHANGED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id="command-pivot",
                    target_host="ubuntu",
                    surface="host",
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id="command-pivot",
                        target_host="ubuntu",
                        surface="network",
                        anchors=("cache",),
                        payload={"kind": "host_discovered"},
                    )
                )
                agent.release.set()
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(service.list_jobs(10)), 1)
            self.assertEqual(agent.contexts[0].trigger_id, "active_host_changed")
            output = "\n".join(captured.output)
            self.assertIn("reason=higher_priority_correlated_candidate", output)
            self.assertIn("higher_priority_trigger=active_host_changed", output)
            self.assertNotIn("event=deferred.stored", output)
        finally:
            agent.release.set()
            service.stop()

    def test_new_command_cancels_only_a_deferred_idle_candidate(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized", "session_idle"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.SESSION_IDLE,
                    source=EventSource.SESSION_RUNTIME,
                    target_host="ubuntu",
                    surface="filesystem",
                )
            )
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_ACCEPTED,
                        source=EventSource.USER_COMMAND,
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={"command": "ls"},
                    )
                )
            agent.release.set()
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(service.list_jobs(10)), 1)
            self.assertIn("event=deferred.cancelled", "\n".join(captured.output))
            self.assertIn("reason=command_accepted", "\n".join(captured.output))
        finally:
            agent.release.set()
            service.stop()

    def test_deferred_release_rechecks_cooldown_without_retrying(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized", "surface_focus"}),
            cooldown_ms=60_000,
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id="command-running",
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))
            for index in range(2):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id=f"command-deferred-{index}",
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={"exit_status": 0, "interrupted": False},
                    )
                )
            self.assertEqual(len(service._deferred_by_session), 1)
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                agent.release.set()
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(service.list_jobs(10)), 1)
            output = "\n".join(captured.output)
            self.assertIn("event=deferred.released", output)
            self.assertIn("reason=cooldown_active", output)
            self.assertIn("event=deferred.discarded", output)
            self.assertNotIn("event=deferred.scheduled", output)
        finally:
            agent.release.set()
            service.stop()

    def test_deferred_candidate_expires_without_becoming_a_job(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    correlation_id="command-running",
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))
            with (
                patch(
                    "dreamland.background_agents.enrichment.service._DEFERRED_MAX_AGE_MS",
                    0,
                ),
                self.assertLogs("dreamland.enrichment", level="INFO") as captured,
            ):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.STATE_MATERIALIZED,
                        source=EventSource.COMMAND_EMULATOR,
                        correlation_id="command-expiring",
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={
                            "kind": "state_materialized",
                            "interaction_focus": {
                                "paths": ("/tmp/expiring",),
                                "primary": {
                                    "kind": "path",
                                    "id": "/tmp/expiring",
                                },
                                "key": "path:/tmp/expiring",
                            },
                        },
                    )
                )
                agent.release.set()
                self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(service.list_jobs(10)), 1)
            output = "\n".join(captured.output)
            self.assertIn("event=deferred.stored", output)
            self.assertIn("event=deferred.expired", output)
            self.assertNotIn("event=deferred.scheduled", output)
        finally:
            agent.release.set()
            service.stop()

    def test_session_close_cancels_deferred_candidate_before_replacement(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized", "session_idle"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="filesystem",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.SESSION_IDLE,
                    source=EventSource.SESSION_RUNTIME,
                    target_host="ubuntu",
                    surface="filesystem",
                )
            )
            replacement = SessionState(
                username="bob",
                langfuse_trace_id="session-1",
                world_state=default_world_state(),
            )
            with self.assertLogs("dreamland.enrichment", level="DEBUG") as captured:
                self.registry.unregister("session-1", session=self.session)
                self.registry.register(replacement)
            agent.release.set()
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(service.list_jobs(10)), 1)
            output = "\n".join(captured.output)
            self.assertIn("event=deferred.cancelled", output)
            self.assertIn("reason=session_closing", output)
        finally:
            agent.release.set()
            service.stop()

    def test_surface_focus_carries_structured_interaction_focus_to_graph(self):
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"surface_focus"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            focus = {
                "cwd": "/home/alice",
                "result_cwd": "/home/alice",
                "paths": ("/home/alice/reports",),
                "primary": {"kind": "path", "id": "/home/alice/reports"},
                "key": "path:/home/alice/reports",
            }
            for command_id in ("command-focus-1", "command-focus-2"):
                self.event_bus.publish(
                    new_session_event(
                        session_id="session-1",
                        event_type=SessionEventType.COMMAND_COMPLETED,
                        source=EventSource.COMMAND_EMULATOR,
                        command_id=command_id,
                        target_host="ubuntu",
                        surface="filesystem",
                        payload={
                            "exit_status": 0,
                            "interaction_focus": focus,
                        },
                    )
                )
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(len(agent.contexts), 1)
            self.assertEqual(
                agent.contexts[0].interaction_focus["paths"],
                ["/home/alice/reports"],
            )
            self.assertEqual(
                agent.contexts[0].interaction_focus["key"],
                "path:/home/alice/reports",
            )
        finally:
            service.stop()

    def test_many_discovery_anchors_are_bounded_with_target_retained(self):
        host_names = tuple(f"host-{index:02d}" for index in range(10))
        hosts = self.session.world_state["hosts_by_name"]
        for host_name in host_names:
            hosts[host_name] = {"hostname": host_name, "facts_by_id": {}}
        agent = ProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"host_discovered"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="network",
                    anchors=host_names,
                    payload={"kind": "host_discovered"},
                )
            )
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(service.list_jobs(1)[0].status, "shadow")
            self.assertEqual(agent.contexts[0].target_host, host_names[0])
            self.assertEqual(agent.contexts[0].anchors[0], host_names[0])
            self.assertEqual(len(agent.contexts[0].anchors), 8)
        finally:
            service.stop()

    def test_queued_job_is_bound_to_the_registration_present_at_acceptance(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, apply_mode="safe")
        second_session = SessionState(
            username="bob",
            langfuse_trace_id="session-2",
            world_state=default_world_state(),
        )
        service.start()
        self.registry.register(self.session)
        old_registration = self.registry.register(second_session)
        try:
            first = service.submit_manual("session-1")
            self.assertTrue(first.accepted)
            self.assertTrue(agent.started.wait(timeout=1))
            second = service.submit_manual("session-2")
            self.assertTrue(second.accepted)

            self.registry.unregister("session-2", session=second_session)
            replacement = SessionState(
                username="carol",
                langfuse_trace_id="session-2",
                world_state=default_world_state(),
            )
            self.registry.register(replacement)
            agent.release.set()
            self.assertTrue(service.wait_until_idle())

            jobs = {job.job_id: job for job in service.list_jobs(10)}
            self.assertEqual(jobs[second.job_id].status, "skipped")
            self.assertEqual(
                jobs[second.job_id].reason,
                "session_closing",
            )
            replacement_facts = replacement.world_state["hosts_by_name"]["ubuntu"][
                "facts_by_id"
            ]
            self.assertNotIn("ambient:process-supervisor", replacement_facts)
            self.assertIsNot(self.registry.get("session-2"), old_registration)
        finally:
            agent.release.set()
            service.stop()

    def test_switching_releases_cancelled_payloads_and_bounds_stalled_executor_queue(
        self,
    ):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent)
        service.start()
        self.registry.register(self.session)
        try:
            self.assertTrue(service.submit_manual("session-1").accepted)
            self.assertTrue(agent.started.wait(timeout=1))
            submitted = 0
            suppressed = 0
            for index in range(service._max_scheduled_jobs * 2):
                session = SessionState(
                    username="alice",
                    langfuse_trace_id=f"switch-{index}",
                    world_state=default_world_state(),
                )
                self.registry.register(session)
                submission = service.submit_manual(session.langfuse_trace_id)
                self.registry.unregister(session.langfuse_trace_id, session=session)
                if submission.accepted:
                    submitted += 1
                    job = service._scheduled_jobs[submission.job_id]
                    self.assertTrue(job.future.cancelled())
                    self.assertIsNone(job.registration)
                    self.assertIsNone(job.candidate)
                else:
                    suppressed += 1
                    self.assertEqual(submission.reason, "worker_queue_full")
                self.assertLessEqual(
                    len(service._scheduled_jobs), service._max_scheduled_jobs
                )
            self.assertEqual(submitted, service._max_scheduled_jobs - 1)
            self.assertGreater(suppressed, 0)
            agent.release.set()
            self.assertTrue(service.wait_until_idle())
            service.stop()
            self.assertEqual(service._scheduled_jobs, {})
            self.assertEqual(len(agent.contexts), 1)
        finally:
            agent.release.set()
            service.stop()

    def test_nonwaiting_stop_terminalizes_cancelled_queued_jobs(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, apply_mode="shadow")
        second_session = SessionState(
            username="bob",
            langfuse_trace_id="session-2",
            world_state=default_world_state(),
        )
        service.start()
        self.registry.register(self.session)
        self.registry.register(second_session)
        try:
            self.assertTrue(service.submit_manual("session-1").accepted)
            self.assertTrue(agent.started.wait(timeout=1))
            second = service.submit_manual("session-2")
            self.assertTrue(second.accepted)

            service.stop(wait=False)

            self.assertTrue(_wait_for_job_status(service, second.job_id, "skipped"))
            jobs = {job.job_id: job for job in service.list_jobs(10)}
            self.assertEqual(jobs[second.job_id].reason, "runtime_stopped")
        finally:
            agent.release.set()
            self.assertTrue(service.wait_until_idle())
            service.stop()

    def test_nonwaiting_restart_rejects_patch_from_prior_runtime(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        try:
            first = service.submit_manual("session-1", surface="process")
            self.assertTrue(first.accepted)
            self.assertTrue(agent.started.wait(timeout=1))

            service.stop(wait=False)
            service.start()
            agent.release.set()
            self.assertTrue(_wait_for_job_status(service, first.job_id, "skipped"))

            jobs = {job.job_id: job for job in service.list_jobs(10)}
            self.assertEqual(jobs[first.job_id].status, "skipped")
            self.assertEqual(jobs[first.job_id].reason, "runtime_restarted")
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
        finally:
            agent.release.set()
            service.stop()

    def test_nonwaiting_restart_ignores_terminal_failure_from_prior_runtime(self):
        agent = BlockingTerminalThenProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="process",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(agent.started.wait(timeout=1))

            service.stop(wait=False)
            service.start()
            agent.release.set()
            first_job_id = service.list_jobs(1)[0].job_id
            self.assertTrue(_wait_for_job_status(service, first_job_id, "skipped"))

            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="network",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(service.wait_until_idle())

            self.assertEqual(agent.calls, 2)
            jobs = service.list_jobs(10)
            self.assertEqual(len(jobs), 2)
            self.assertEqual(jobs[0].status, "shadow")
            self.assertEqual(jobs[1].reason, "runtime_restarted")
        finally:
            agent.release.set()
            service.stop()

    def test_nonwaiting_restart_does_not_carry_stale_inflight_admission(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, apply_mode="shadow")
        service.start()
        self.registry.register(self.session)
        try:
            first = service.submit_manual("session-1", surface="process")
            self.assertTrue(first.accepted)
            self.assertTrue(agent.started.wait(timeout=1))

            service.stop(wait=False)
            service.start()
            second = service.submit_manual("session-1", surface="process")

            self.assertTrue(second.accepted)
            self.assertTrue(_wait_for_job_status(service, second.job_id, "shadow"))
            agent.release.set()
            self.assertTrue(_wait_for_job_status(service, first.job_id, "skipped"))
        finally:
            agent.release.set()
            service.stop()

    def test_prior_runtime_completion_cannot_release_current_deferred_work(self):
        agent = BlockingTwoProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        try:
            first = service.submit_manual("session-1", surface="process")
            self.assertTrue(first.accepted)
            self.assertTrue(agent.started[0].wait(timeout=1))

            service.stop(wait=False)
            service.start()
            second = service.submit_manual("session-1", surface="process")
            self.assertTrue(second.accepted)
            self.assertTrue(agent.started[1].wait(timeout=1))
            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="network",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertIn("session-1", service._deferred_by_session)

            agent.release[0].set()
            self.assertTrue(_wait_for_job_status(service, first.job_id, "skipped"))
            self.assertIn("session-1", service._deferred_by_session)

            entered_release = threading.Event()
            continue_release = threading.Event()
            original_release = service._release_deferred_record

            def blocking_release(record, **kwargs):
                entered_release.set()
                if not continue_release.wait(timeout=2):
                    raise TimeoutError("test deferred release was not signalled")
                return original_release(record, **kwargs)

            with patch.object(
                service,
                "_release_deferred_record",
                side_effect=blocking_release,
            ):
                agent.release[1].set()
                self.assertTrue(entered_release.wait(timeout=1))
                self.assertFalse(service.wait_until_idle(timeout=0.05))
                continue_release.set()
                self.assertTrue(service.wait_until_idle())
            self.assertEqual(agent.calls, 3)
            self.assertEqual(service._deferred_by_session, {})
        finally:
            for release in agent.release:
                release.set()
            service.stop()

    def test_nonwaiting_stop_without_restart_uses_stopped_reason(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, apply_mode="shadow")
        service.start()
        self.registry.register(self.session)
        try:
            submission = service.submit_manual("session-1")
            self.assertTrue(submission.accepted)
            self.assertTrue(agent.started.wait(timeout=1))

            service.stop(wait=False)
            agent.release.set()

            self.assertTrue(_wait_for_job_status(service, submission.job_id, "skipped"))
            job = {item.job_id: item for item in service.list_jobs(10)}[
                submission.job_id
            ]
            self.assertEqual(job.reason, "runtime_stopped")
        finally:
            agent.release.set()
            service.stop()

    def test_start_is_rejected_while_nonwaiting_stop_is_closing_subscription(self):
        service = self._service(apply_mode="shadow")
        service.start()
        subscription = service._subscription
        assert subscription is not None
        entered_close = threading.Event()
        release_close = threading.Event()
        original_close = subscription.close

        def blocking_close():
            entered_close.set()
            if not release_close.wait(timeout=2):
                raise TimeoutError("test close release was not signalled")
            original_close()

        stopper = threading.Thread(target=service.stop, kwargs={"wait": False})
        try:
            with patch.object(subscription, "close", side_effect=blocking_close):
                stopper.start()
                self.assertTrue(entered_close.wait(timeout=1))

                with self.assertRaisesRegex(RuntimeError, "stopping or draining"):
                    service.start()

                release_close.set()
                stopper.join(timeout=2)
            self.assertFalse(stopper.is_alive())
            service.start()
            self.assertTrue(service.status().started)
        finally:
            release_close.set()
            stopper.join(timeout=2)
            service.stop()

    def test_failed_subscription_does_not_partially_start_runtime(self):
        service = self._service(apply_mode="shadow")

        with patch.object(
            self.event_bus,
            "subscribe",
            side_effect=ValueError("synthetic subscription failure"),
        ):
            with self.assertRaisesRegex(ValueError, "synthetic"):
                service.start()

        self.assertFalse(service.status().started)
        self.assertIsNone(service._executor)
        self.assertIsNone(service._subscription)
        self.assertEqual(service._runtime_generation, 0)

    def test_waiting_stop_drains_current_generation_before_returning(self):
        agent = BlockingFirstProposalAgent()
        service = self._service(agent=agent, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        stopper = threading.Thread(target=service.stop, kwargs={"wait": True})
        try:
            submission = service.submit_manual("session-1")
            self.assertTrue(submission.accepted)
            self.assertTrue(agent.started.wait(timeout=1))

            stopper.start()
            deadline = time.monotonic() + 1
            while service.status().started and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertFalse(service.status().started)
            agent.release.set()
            stopper.join(timeout=2)

            self.assertFalse(stopper.is_alive())
            job = {item.job_id: item for item in service.list_jobs(10)}[
                submission.job_id
            ]
            self.assertEqual(job.status, "applied")
            self.assertEqual(job.reason, "applied")
        finally:
            agent.release.set()
            stopper.join(timeout=2)
            service.stop()

    def test_restart_during_pure_apply_cannot_commit_prior_runtime_patch(self):
        entered_apply = threading.Event()
        release_apply = threading.Event()

        def blocking_apply(*args, **kwargs):
            result = apply_enrichment_patch_pure(*args, **kwargs)
            entered_apply.set()
            if not release_apply.wait(timeout=2):
                raise TimeoutError("test apply release was not signalled")
            return result

        service = self._service(agent=ProposalAgent(), apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        try:
            with patch(
                "dreamland.background_agents.enrichment.service.apply_enrichment_patch",
                side_effect=blocking_apply,
            ):
                submission = service.submit_manual("session-1")
                self.assertTrue(submission.accepted)
                self.assertTrue(entered_apply.wait(timeout=1))

                service.stop(wait=False)
                service.start()
                release_apply.set()
                self.assertTrue(
                    _wait_for_job_status(service, submission.job_id, "skipped")
                )

            job = {item.job_id: item for item in service.list_jobs(10)}[
                submission.job_id
            ]
            self.assertEqual(job.reason, "runtime_restarted")
            facts = self.session.world_state["hosts_by_name"]["ubuntu"]["facts_by_id"]
            self.assertNotIn("ambient:process-supervisor", facts)
        finally:
            release_apply.set()
            service.stop()

    def test_pre_restart_event_delivery_cannot_open_new_runtime_circuit(self):
        agent = ProviderAwareProposalAgent()
        service = self._service(
            agent=agent,
            enabled=True,
            apply_mode="shadow",
            triggers=frozenset({"state_materialized"}),
        )
        service.start()
        self.registry.register(self.session)
        entered_observer = threading.Event()
        release_observer = threading.Event()
        original_observer = service._observe_foreground_provider_failure

        def blocking_observer(event, *, runtime_generation):
            entered_observer.set()
            if not release_observer.wait(timeout=2):
                raise TimeoutError("test observer release was not signalled")
            return original_observer(
                event,
                runtime_generation=runtime_generation,
            )

        failure_event = new_session_event(
            session_id="session-1",
            event_type=SessionEventType.COMMAND_COMPLETED,
            source=EventSource.COMMAND_EMULATOR,
            target_host="ubuntu",
            surface="process",
            payload={
                "provider_failure": {
                    "kind": "provider_credit_exhausted",
                    "disposition": "terminal",
                    "provider": "openai",
                }
            },
        )
        publisher = threading.Thread(
            target=self.event_bus.publish, args=(failure_event,)
        )
        try:
            with patch.object(
                service,
                "_observe_foreground_provider_failure",
                side_effect=blocking_observer,
            ):
                publisher.start()
                self.assertTrue(entered_observer.wait(timeout=1))
                service.stop(wait=False)
                service.start()
                release_observer.set()
                publisher.join(timeout=2)

            self.event_bus.publish(
                new_session_event(
                    session_id="session-1",
                    event_type=SessionEventType.STATE_MATERIALIZED,
                    source=EventSource.COMMAND_EMULATOR,
                    target_host="ubuntu",
                    surface="network",
                    payload={"kind": "state_materialized"},
                )
            )
            self.assertTrue(service.wait_until_idle())

            self.assertFalse(publisher.is_alive())
            self.assertEqual(len(agent.contexts), 1)
        finally:
            release_observer.set()
            publisher.join(timeout=2)
            service.stop()

    def test_missing_manual_target_is_a_structured_skip(self):
        service = self._service(enabled=False, apply_mode="safe")
        service.start()
        self.registry.register(self.session)
        try:
            with self.assertLogs("dreamland.enrichment", level="INFO") as captured:
                submission = service.submit_manual(
                    "session-1",
                    target_host="missing-host",
                )
                self.assertTrue(service.wait_until_idle())

            self.assertTrue(submission.accepted)
            job = service.list_jobs(1)[0]
            self.assertEqual(job.status, "skipped")
            self.assertEqual(job.reason, "target_missing")
            output = "\n".join(captured.output)
            self.assertIn("event=snapshot.rejected", output)
            self.assertNotIn("event=job.exception", output)
        finally:
            service.stop()

    def test_control_status_missing_session_and_graph_are_available(self):
        service = self._service(agent=ProposalAgent())
        self.assertFalse(service.status().started)
        self.assertEqual(
            service.submit_manual("missing").reason,
            "runtime_not_started",
        )
        service.start()
        try:
            submission = service.submit_manual("missing")
            self.assertFalse(submission.accepted)
            self.assertEqual(submission.reason, "session_missing")
            graph = service.graph_mermaid()
            self.assertIn("Main enrichment proposal graph", graph)
            self.assertIn("coordinator", graph)
            self.assertIn("Bounded Integrator refresh graph", graph)
            self.assertIn("snapshot_refresh", graph)
        finally:
            service.stop()


def _wait_for_job_count(
    service: EnrichmentRuntimeService,
    expected: int,
    timeout: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(service.list_jobs(expected)) >= expected:
            return True
        time.sleep(0.005)
    return False


def _wait_for_job_status(
    service: EnrichmentRuntimeService,
    job_id: str | None,
    expected: str,
    timeout: float = 1.0,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        jobs = {job.job_id: job for job in service.list_jobs(20)}
        if job_id in jobs and jobs[job_id].status == expected:
            return True
        time.sleep(0.005)
    return False


if __name__ == "__main__":
    unittest.main()

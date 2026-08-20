from dataclasses import replace

from docker.client import DockerClient
from docker.errors import DockerException

from app.containers.git import describe_git_failure
from app.controller.store import ControllerStore, SandboxAdmissionError
from app.controller.store.lifecycle_status import SandboxLifecycleStatus
from app.platform.naming import workspace_volume
from app.previews.config import get_preview_settings
from app.sandboxes.lifecycle import (
    lifecycle_conflict_detail,
    lifecycle_lease,
    project_mirror_lock,
)
from app.sandboxes.manifest import (
    read_manifest,
    transition_sandbox_lifecycle,
    write_manifest,
)
from app.sandboxes.publish import (
    GitHubApiError,
    PublishError,
    discover_or_create_pull_request,
    publish_reviewed_feature,
    reviewed_target,
)

from .coercion import _base_branch, _optional_string, _required_sync_value, require_v1
from .errors import SandboxConflict, SandboxDependencyFailure
from .outcomes import PublishOutcome


def publish(
    docker_client: DockerClient,
    controller_store: ControllerStore,
    *,
    sandbox_id: str,
    stop_blocking_preview: bool,
) -> PublishOutcome:
    """Push one reviewed branch, then discover or create and verify its PR."""
    sandbox = controller_store.sandbox(sandbox_id)
    require_v1(
        sandbox, sandbox_id, "cannot publish to a remote; recreate it explicitly as v1."
    )
    assert sandbox is not None
    manifest = read_manifest(controller_store, sandbox_id)
    if (
        manifest is None
        or manifest.lifecycle_status is not SandboxLifecycleStatus.READY
    ):
        raise SandboxConflict("Sandbox can publish only from ready")
    if not manifest.feature_branch:
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no feature branch; recreate it explicitly to publish."
        )
    project = controller_store.project(str(sandbox["project_id"]))
    if (
        project is None
        or not project.get("remote_url")
        or not project.get("mirror_volume")
    ):
        raise SandboxConflict(
            f"Sandbox '{sandbox_id}' has no project mirror or remote; recreate it explicitly to publish."
        )
    prior_publication = controller_store.sandbox_publication(sandbox_id) or {}
    remote_branch = _optional_string(prior_publication.get("remote_branch")) or (
        manifest.remote_branch or manifest.feature_branch
    )
    # Which planning session this push carries. Recorded on the publication so
    # the feature status of a *different* session in the same sandbox does not
    # inherit this pull request.
    owner_session_id = controller_store.publication_owner_session(sandbox_id)
    base_ref = _required_sync_value(sandbox, "base_ref", sandbox_id)
    workspace_name = workspace_volume(sandbox_id)
    mirror_name = str(project["mirror_volume"])
    preview_settings = get_preview_settings()

    try:
        # This reads the stored review only. It must run before the lifecycle
        # lease, which can stop a requested blocking preview.
        reviewed_target(
            controller_store,
            sandbox_id=sandbox_id,
            feature_branch=manifest.feature_branch,
        )
        # Lock order is fixed: sandbox lifecycle lease, then project mirror
        # lock. The mirror lock covers both local staging and network push.
        with lifecycle_lease(
            controller_store,
            sandbox_id,
            "publish",
            docker_client=docker_client,
            stop_blocking_previews=stop_blocking_preview,
        ) as lease:
            if lease is None:
                raise RuntimeError("managed sandbox did not acquire a lifecycle lease")
            operation_id = str(lease["operation_id"])
            transition_sandbox_lifecycle(
                controller_store,
                replace(
                    manifest,
                    last_error=None,
                ),
                to_status=SandboxLifecycleStatus.PUBLISHING,
            )
            try:
                with project_mirror_lock(
                    controller_store,
                    str(project["id"]),
                    "publish",
                    operation_id=operation_id,
                ):
                    outcome = publish_reviewed_feature(
                        docker_client,
                        store=controller_store,
                        preview_settings=preview_settings,
                        sandbox_id=sandbox_id,
                        workspace_volume=workspace_name,
                        mirror_volume=mirror_name,
                        feature_branch=manifest.feature_branch,
                        remote_branch=remote_branch,
                    )
            except Exception as error:
                failure_message = describe_git_failure(error)
                prior = controller_store.sandbox_publication(sandbox_id) or {}
                controller_store.record_sandbox_publication(
                    sandbox_id=sandbox_id,
                    remote_branch=remote_branch,
                    last_pushed_commit=_optional_string(
                        prior.get("last_pushed_commit")
                    ),
                    remote_branch_sha=_optional_string(prior.get("remote_branch_sha")),
                    last_error=failure_message,
                )
                failed = read_manifest(controller_store, sandbox_id)
                if failed is not None:
                    transition_sandbox_lifecycle(
                        controller_store,
                        replace(
                            failed,
                            last_error=failure_message,
                        ),
                        to_status=SandboxLifecycleStatus.READY,
                    )
                raise
            publication = controller_store.record_sandbox_publication(
                sandbox_id=sandbox_id,
                remote_branch=outcome.remote_branch,
                last_pushed_commit=outcome.last_pushed_commit,
                remote_branch_sha=outcome.remote_branch_sha,
                last_error=None,
                session_id=owner_session_id,
            )
            pushed = read_manifest(controller_store, sandbox_id)
            if pushed is None:
                raise RuntimeError("sandbox manifest disappeared during publish")
            write_manifest(
                controller_store,
                replace(
                    pushed,
                    last_error=None,
                ),
            )
            try:
                pull_request = discover_or_create_pull_request(
                    remote_url=str(project["remote_url"]),
                    remote_branch=outcome.remote_branch,
                    base_branch=_base_branch(base_ref),
                    title=manifest.feature_title
                    or manifest.feature_key
                    or outcome.remote_branch,
                )
            except (GitHubApiError, PublishError) as error:
                # Git and GitHub are independent systems. The successful push
                # remains observed evidence; the safe error text is retryable.
                controller_store.record_sandbox_publication(
                    sandbox_id=sandbox_id,
                    remote_branch=outcome.remote_branch,
                    last_pushed_commit=outcome.last_pushed_commit,
                    remote_branch_sha=outcome.remote_branch_sha,
                    last_error=str(error),
                )
                failed = read_manifest(controller_store, sandbox_id)
                if failed is not None:
                    transition_sandbox_lifecycle(
                        controller_store,
                        replace(
                            failed,
                            last_error=str(error),
                        ),
                        to_status=SandboxLifecycleStatus.READY,
                    )
                raise PublishError(424, str(error)) from error
            publication = controller_store.record_sandbox_publication(
                sandbox_id=sandbox_id,
                remote_branch=outcome.remote_branch,
                last_pushed_commit=outcome.last_pushed_commit,
                remote_branch_sha=outcome.remote_branch_sha,
                pr_number=pull_request.number,
                pr_url=pull_request.url,
                pr_state=pull_request.state,
                pr_merged_at=pull_request.merged_at,
                last_error=None,
            )
            completed = read_manifest(controller_store, sandbox_id)
            if completed is None:
                raise RuntimeError("sandbox manifest disappeared during publish")
            transition_sandbox_lifecycle(
                controller_store,
                replace(
                    completed,
                    last_error=None,
                ),
                to_status=SandboxLifecycleStatus.READY,
            )
            return PublishOutcome(
                sandbox_id=sandbox_id,
                operation_id=operation_id,
                remote_branch=outcome.remote_branch,
                last_pushed_commit=outcome.last_pushed_commit,
                remote_branch_sha=outcome.remote_branch_sha,
                pushed=outcome.pushed,
                pr_number=publication.get("pr_number"),
                pr_url=_optional_string(publication.get("pr_url")),
                pr_state=_optional_string(publication.get("pr_state")),
                pr_merged_at=_optional_string(publication.get("pr_merged_at")),
            )
    except SandboxAdmissionError as error:
        raise SandboxConflict(lifecycle_conflict_detail(error)) from error
    except PublishError:
        raise
    except (DockerException, RuntimeError, ValueError) as error:
        raise SandboxDependencyFailure(describe_git_failure(error)) from error

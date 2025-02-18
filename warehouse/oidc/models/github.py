# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from sqlalchemy import Column, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from warehouse.oidc.models._core import (
    OIDCPublisher,
    PendingOIDCPublisher,
    _check_claim_binary,
)


def _check_job_workflow_ref(ground_truth, signed_claim, all_signed_claims):
    # We expect a string formatted as follows:
    #   OWNER/REPO/.github/workflows/WORKFLOW.yml@REF
    # where REF is the value of the `ref` claim.

    # Defensive: GitHub should never give us an empty job_workflow_ref,
    # but we check for one anyways just in case.
    if not signed_claim:
        return False

    ref = all_signed_claims.get("ref")
    if not ref:
        return False

    return f"{ground_truth}@{ref}" == signed_claim


def _check_environment(ground_truth, signed_claim, all_signed_claims):
    # When there is an environment, we expect a case-insensitive string.
    # https://docs.github.com/en/actions/deployment/targeting-different-environments/using-environments-for-deployment
    # For tokens that are generated outside of an environment, the claim will
    # be missing.

    # If we haven't set an environment name for the publisher, we don't need to
    # check this claim
    if ground_truth is None:
        return True

    # Defensive: GitHub might give us an empty environment if this token wasn't
    # generated from within an environment, in which case the check should
    # fail.
    if not signed_claim:
        return False

    # We store the normalized environment name, but we normalize both here to
    # ensure we can't accidentally become case-sensitive.
    return ground_truth.lower() == signed_claim.lower()


def _check_sub(ground_truth, signed_claim, _all_signed_claims):
    # We expect a string formatted as follows:
    #  repo:ORG/REPO[:OPTIONAL-STUFF]
    # where :OPTIONAL-STUFF is a concatenation of other job context
    # metadata. We currently lack the ground context to verify that
    # additional metadata, so we limit our verification to just the ORG/REPO
    # component.

    # Defensive: GitHub should never give us an empty subject.
    if not signed_claim:
        return False

    components = signed_claim.split(":")
    if len(components) < 2:
        return False

    return f"{components[0]}:{components[1]}" == ground_truth


class GitHubPublisherMixin:
    """
    Common functionality for both pending and concrete GitHub OIDC publishers.
    """

    repository_name = Column(String, nullable=False)
    repository_owner = Column(String, nullable=False)
    repository_owner_id = Column(String, nullable=False)
    workflow_filename = Column(String, nullable=False)
    environment = Column(String, nullable=True)

    __required_verifiable_claims__ = {
        "sub": _check_sub,
        "repository": _check_claim_binary(str.__eq__),
        "repository_owner": _check_claim_binary(str.__eq__),
        "repository_owner_id": _check_claim_binary(str.__eq__),
        "job_workflow_ref": _check_job_workflow_ref,
    }

    __optional_verifiable_claims__ = {
        "environment": _check_environment,
    }

    __unchecked_claims__ = {
        "actor",
        "actor_id",
        "jti",
        "ref",
        "sha",
        "run_id",
        "run_number",
        "run_attempt",
        "head_ref",
        "base_ref",
        "event_name",
        "ref_type",
        "repository_id",
        "workflow",
        "repository_visibility",
        "workflow_sha",
        "job_workflow_sha",
        "workflow_ref",
        "runner_environment",
        "environment_node_id",
        "enterprise",
    }

    @property
    def _workflow_slug(self):
        return f".github/workflows/{self.workflow_filename}"

    @property
    def publisher_name(self):
        return "GitHub"

    @property
    def repository(self):
        return f"{self.repository_owner}/{self.repository_name}"

    @property
    def publisher_url(self):
        return f"https://github.com/{self.repository}"

    @property
    def job_workflow_ref(self):
        return f"{self.repository}/{self._workflow_slug}"

    @property
    def sub(self):
        return f"repo:{self.repository}"

    def __str__(self):
        return self.workflow_filename


class GitHubPublisher(GitHubPublisherMixin, OIDCPublisher):
    __tablename__ = "github_oidc_publishers"
    __mapper_args__ = {"polymorphic_identity": "github_oidc_publishers"}
    __table_args__ = (
        UniqueConstraint(
            "repository_name",
            "repository_owner",
            "workflow_filename",
            "environment",
            name="_github_oidc_publisher_uc",
        ),
    )

    id = Column(UUID(as_uuid=True), ForeignKey(OIDCPublisher.id), primary_key=True)


class PendingGitHubPublisher(GitHubPublisherMixin, PendingOIDCPublisher):
    __tablename__ = "pending_github_oidc_publishers"
    __mapper_args__ = {"polymorphic_identity": "pending_github_oidc_publishers"}
    __table_args__ = (
        UniqueConstraint(
            "repository_name",
            "repository_owner",
            "workflow_filename",
            "environment",
            name="_pending_github_oidc_publisher_uc",
        ),
    )

    id = Column(
        UUID(as_uuid=True), ForeignKey(PendingOIDCPublisher.id), primary_key=True
    )

    def reify(self, session):
        """
        Returns a `GitHubPublisher` for this `PendingGitHubPublisher`,
        deleting the `PendingGitHubPublisher` in the process.
        """

        maybe_publisher = (
            session.query(GitHubPublisher)
            .filter(
                GitHubPublisher.repository_name == self.repository_name,
                GitHubPublisher.repository_owner == self.repository_owner,
                GitHubPublisher.workflow_filename == self.workflow_filename,
                GitHubPublisher.environment == self.environment,
            )
            .one_or_none()
        )

        publisher = maybe_publisher or GitHubPublisher(
            repository_name=self.repository_name,
            repository_owner=self.repository_owner,
            repository_owner_id=self.repository_owner_id,
            workflow_filename=self.workflow_filename,
            environment=self.environment,
        )

        session.delete(self)
        return publisher

"""Tests for PolicyMixin storage methods."""

import pytest


class TestPolicyMixin:
    @pytest.mark.asyncio
    async def test_add_and_list(self, storage):
        pid = await storage.policy_add("cleanup", "auto-expire", {"max_age_days": 90})
        assert pid

        policies = await storage.policy_list()
        assert len(policies) == 1
        assert policies[0]["name"] == "cleanup"
        assert policies[0]["policy_type"] == "auto-expire"
        assert policies[0]["config"]["max_age_days"] == 90
        assert policies[0]["enabled"] is True

    @pytest.mark.asyncio
    async def test_add_with_namespace(self, storage):
        await storage.policy_add("ns-policy", "auto-tag", {"tags": ["old"]}, namespace_filter="archive")
        policy = await storage.policy_get("ns-policy")
        assert policy is not None
        assert policy["namespace_filter"] == "archive"

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, storage):
        result = await storage.policy_get("no-such-policy")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete(self, storage):
        await storage.policy_add("temp", "auto-archive", {})
        deleted = await storage.policy_delete("temp")
        assert deleted is True

        policies = await storage.policy_list()
        assert len(policies) == 0

    @pytest.mark.asyncio
    async def test_delete_nonexistent(self, storage):
        deleted = await storage.policy_delete("ghost")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_update_last_run(self, storage):
        await storage.policy_add("runner", "auto-expire", {})
        await storage.policy_update_last_run("runner")

        policy = await storage.policy_get("runner")
        assert policy["last_run_at"] is not None

    @pytest.mark.asyncio
    async def test_get_enabled(self, storage):
        await storage.policy_add("active", "auto-tag", {})
        enabled = await storage.policy_get_enabled()
        assert len(enabled) == 1
        assert enabled[0]["name"] == "active"

    @pytest.mark.asyncio
    async def test_multiple_policies(self, storage):
        await storage.policy_add("p1", "auto-expire", {"days": 30})
        await storage.policy_add("p2", "auto-tag", {"tags": ["review"]})
        await storage.policy_add("p3", "auto-archive", {})

        policies = await storage.policy_list()
        assert len(policies) == 3

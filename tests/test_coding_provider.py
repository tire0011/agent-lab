"""Unit tests for modules.coding_provider — durable provider rotation.

No CLI is ever launched here: availability is faked so the tests assert the
selection and persistence rules directly.
"""

import os
import tempfile
import unittest
from unittest.mock import patch

from modules.claude_worker import ClaudeWorker
from modules.coding_provider import (
    ENV_PROVIDER_ALLOW_LIST,
    ENV_PROVIDER_PIN,
    KNOWN_PROVIDERS,
    PROVIDER_CLAUDE,
    PROVIDER_CODEX,
    UnknownProviderError,
    allowed_providers,
    available_providers,
    build_random_worker,
    build_worker,
    choose_provider,
    is_available,
    pinned_provider,
    provider_of,
    random_worker_factory,
)
from modules.codex_worker import CodexWorker


def _clean_env(**overrides):
    """Environment without provider pins, plus the given overrides."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (ENV_PROVIDER_PIN, ENV_PROVIDER_ALLOW_LIST)
    }
    env.update(overrides)
    return patch.dict(os.environ, env, clear=True)


def _installed(*providers):
    """Pretend exactly *providers* have their CLI installed."""
    return patch(
        "modules.coding_provider.is_available",
        side_effect=lambda provider: provider in providers,
    )


class AllowedProvidersTests(unittest.TestCase):
    def test_both_agents_are_allowed_by_default(self):
        with _clean_env():
            self.assertEqual(allowed_providers(), list(KNOWN_PROVIDERS))

    def test_allow_list_narrows_and_orders(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "claude"}):
            self.assertEqual(allowed_providers(), [PROVIDER_CLAUDE])

    def test_allow_list_is_case_and_space_insensitive(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: " Claude , CODEX "}):
            self.assertEqual(allowed_providers(), [PROVIDER_CLAUDE, PROVIDER_CODEX])

    def test_duplicates_are_collapsed(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "codex,codex"}):
            self.assertEqual(allowed_providers(), [PROVIDER_CODEX])

    def test_unknown_entries_are_dropped(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "codex,gpt5-cli"}):
            self.assertEqual(allowed_providers(), [PROVIDER_CODEX])

    def test_an_entirely_unknown_list_falls_back_to_all_known(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "nonsense"}):
            self.assertEqual(allowed_providers(), list(KNOWN_PROVIDERS))

    def test_empty_list_falls_back_to_all_known(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "  "}):
            self.assertEqual(allowed_providers(), list(KNOWN_PROVIDERS))


class AvailabilityTests(unittest.TestCase):
    def test_availability_follows_the_binary(self):
        with _clean_env(), patch(
            "modules.coding_provider.shutil.which", return_value="/usr/bin/claude"
        ):
            self.assertTrue(is_available(PROVIDER_CLAUDE))
        with _clean_env(), patch("modules.coding_provider.shutil.which", return_value=None):
            self.assertFalse(is_available(PROVIDER_CLAUDE))

    def test_unknown_provider_is_never_available(self):
        self.assertFalse(is_available("gpt5-cli"))

    def test_available_providers_is_the_installed_subset(self):
        with _clean_env(), _installed(PROVIDER_CLAUDE):
            self.assertEqual(available_providers(), [PROVIDER_CLAUDE])


class PinTests(unittest.TestCase):
    def test_pin_is_honoured(self):
        with _clean_env(**{ENV_PROVIDER_PIN: "claude"}):
            self.assertEqual(pinned_provider(), PROVIDER_CLAUDE)
            self.assertEqual(choose_provider(), PROVIDER_CLAUDE)

    def test_pin_wins_over_a_random_draw(self):
        with _clean_env(**{ENV_PROVIDER_PIN: "codex"}), _installed(PROVIDER_CLAUDE):
            self.assertEqual(choose_provider(), PROVIDER_CODEX)

    def test_unknown_pin_is_ignored(self):
        with _clean_env(**{ENV_PROVIDER_PIN: "gpt5-cli"}):
            self.assertIsNone(pinned_provider())

    def test_no_pin_by_default(self):
        with _clean_env():
            self.assertIsNone(pinned_provider())


class PersistentDatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "agent.db")

    def tearDown(self):
        self._tmp.cleanup()


class ChooseProviderTests(PersistentDatabaseTestCase):
    def test_consecutive_selections_alternate(self):
        with _clean_env(), _installed(*KNOWN_PROVIDERS):
            selected = [choose_provider(db_path=self.db_path) for _ in range(4)]
        self.assertEqual(selected, [PROVIDER_CODEX, PROVIDER_CLAUDE] * 2)

    def test_rotation_persists_across_fresh_callers(self):
        with _clean_env(), _installed(*KNOWN_PROVIDERS):
            first = choose_provider(db_path=self.db_path)
            # choose_provider opens and closes its own connection each time,
            # which models a process/service restart.
            after_restart = choose_provider(db_path=self.db_path)
        self.assertEqual((first, after_restart), (PROVIDER_CODEX, PROVIDER_CLAUDE))

    def test_an_uninstalled_agent_is_never_drawn(self):
        with _clean_env(), _installed(PROVIDER_CODEX):
            drawn = {choose_provider(db_path=self.db_path) for _ in range(4)}
        self.assertEqual(drawn, {PROVIDER_CODEX})

    def test_a_disallowed_agent_is_never_drawn(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "codex"}), _installed(*KNOWN_PROVIDERS):
            drawn = {choose_provider(db_path=self.db_path) for _ in range(4)}
        self.assertEqual(drawn, {PROVIDER_CODEX})

    def test_falls_back_to_the_first_allowed_agent_when_none_is_installed(self):
        with _clean_env(), _installed():
            self.assertEqual(choose_provider(db_path=self.db_path), KNOWN_PROVIDERS[0])

    def test_fallback_respects_the_allow_list(self):
        with _clean_env(**{ENV_PROVIDER_ALLOW_LIST: "claude"}), _installed():
            self.assertEqual(choose_provider(db_path=self.db_path), PROVIDER_CLAUDE)

    def test_pin_does_not_advance_rotation(self):
        with _clean_env(), _installed(*KNOWN_PROVIDERS):
            self.assertEqual(choose_provider(db_path=self.db_path), PROVIDER_CODEX)
        with _clean_env(**{ENV_PROVIDER_PIN: "codex"}), _installed(*KNOWN_PROVIDERS):
            self.assertEqual(choose_provider(db_path=self.db_path), PROVIDER_CODEX)
        with _clean_env(), _installed(*KNOWN_PROVIDERS):
            self.assertEqual(choose_provider(db_path=self.db_path), PROVIDER_CLAUDE)


class BuildWorkerTests(PersistentDatabaseTestCase):
    def test_builds_the_matching_worker(self):
        self.assertIsInstance(build_worker(PROVIDER_CODEX), CodexWorker)
        self.assertIsInstance(build_worker(PROVIDER_CLAUDE), ClaudeWorker)

    def test_provider_name_is_normalized(self):
        self.assertIsInstance(build_worker(" Claude "), ClaudeWorker)

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(UnknownProviderError):
            build_worker("gpt5-cli")

    def test_keyword_arguments_reach_the_worker(self):
        worker = build_worker(PROVIDER_CLAUDE, db_path=None, model="opus")
        self.assertEqual(worker.model, "opus")

    def test_random_worker_reports_its_provider(self):
        with _clean_env(), _installed(PROVIDER_CLAUDE):
            provider, worker = build_random_worker(db_path=self.db_path)
        self.assertEqual(provider, PROVIDER_CLAUDE)
        self.assertEqual(worker.provider, PROVIDER_CLAUDE)


class WorkerFactoryTests(PersistentDatabaseTestCase):
    def test_one_agent_per_working_directory(self):
        with _clean_env(), _installed(*KNOWN_PROVIDERS):
            factory = random_worker_factory(db_path=self.db_path)
            first = factory("/worktrees/exp-1").provider
            again = factory("/worktrees/exp-1").provider
        self.assertEqual(first, again)

    def test_different_working_directories_draw_independently(self):
        with _clean_env(), _installed(*KNOWN_PROVIDERS):
            factory = random_worker_factory(db_path=self.db_path)
            drawn = [factory(f"/worktrees/exp-{i}").provider for i in range(4)]
        self.assertEqual(drawn, [PROVIDER_CODEX, PROVIDER_CLAUDE] * 2)

    def test_non_sticky_factory_draws_every_call(self):
        with _clean_env(), _installed(*KNOWN_PROVIDERS):
            factory = random_worker_factory(db_path=self.db_path, sticky=False)
            drawn = [factory("/worktrees/exp-1").provider for _ in range(4)]
        self.assertEqual(drawn, [PROVIDER_CODEX, PROVIDER_CLAUDE] * 2)


class ProviderOfTests(unittest.TestCase):
    def test_reads_a_workers_provider(self):
        self.assertEqual(provider_of(CodexWorker()), PROVIDER_CODEX)
        self.assertEqual(provider_of(ClaudeWorker()), PROVIDER_CLAUDE)

    def test_anything_else_yields_the_default(self):
        from unittest.mock import MagicMock

        self.assertIsNone(provider_of(MagicMock()))
        self.assertIsNone(provider_of(object()))
        self.assertEqual(provider_of(object(), PROVIDER_CODEX), PROVIDER_CODEX)


if __name__ == "__main__":
    unittest.main()

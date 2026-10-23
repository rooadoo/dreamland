import unittest

from dreamland.background_agents.surfaces import infer_command_surface


class BackgroundAgentSurfaceTests(unittest.TestCase):
    def test_recognizes_common_command_surfaces(self):
        cases = {
            "ss -lntp": "network",
            "sudo systemctl status redis": "service",
            "cat /etc/redis/redis.conf": "filesystem",
            "redis-cli INFO": "database",
            "whoami": "identity",
            "apt list --installed": "packages",
            "echo hello": "shell",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(infer_command_surface(command), expected)

    def test_pipeline_output_retains_only_the_general_shell_surface(self):
        self.assertEqual(infer_command_surface("/bin/ps aux | grep redis"), "shell")


if __name__ == "__main__":
    unittest.main()

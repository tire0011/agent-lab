from pathlib import Path

path = Path(__file__).with_name("apply_issue_111.py")
source = path.read_text(encoding="utf-8")
start = source.index("# Update the two prompt-specific assertions")
end = source.index("NEW_TEST.write_text", start)
replacement = '''# Update the two prompt-specific assertions that intentionally describe the old protocol.
t = TEST_MAIN.read_text(encoding="utf-8")
t = replace_once(
    t,
    "self.assertIn('\\\"tool\\\":\\\"find_tool\\\"', client.prompts[0])".replace("\\\\\\\"", "\\\""),
    "self.assertIn('\\\"action\\\":\\\"find_tool\\\"', client.prompts[0])\\n        self.assertNotIn('\\\"type\\\":\\\"tool\\\"', client.prompts[0])".replace("\\\\\\\"", "\\\""),
    "main prompt assertion",
)
TEST_MAIN.write_text(t, encoding="utf-8")

t = TEST_COMMAND.read_text(encoding="utf-8")
t = replace_once(
    t,
    "self.assertIn('{\\\"type\\\":\\\"tool\\\",\\\"tool\\\":\\\"command\\\"', prompt)".replace("\\\\\\\"", "\\\""),
    "self.assertIn('{\\\"action\\\":\\\"command\\\"', prompt)\\n            self.assertNotIn('{\\\"type\\\":\\\"tool\\\"', prompt)".replace("\\\\\\\"", "\\\""),
    "command prompt assertion",
)
TEST_COMMAND.write_text(t, encoding="utf-8")

'''
source = source[:start] + replacement + source[end:]
namespace = {"__file__": str(path), "__name__": "__main__"}
exec(compile(source, str(path), "exec"), namespace)

from agent.env import get_openrouter_key, load_env, parse_env_text


def test_parse_forms():
    text = (
        "# comment\n"
        "\n"
        'KEY1 = "value one"   \n'
        "KEY2='v2'\n"
        "export KEY3=v3   \n"
        'KEY4 = "quoted"  # trailing comment\n'
        "KEY5=plain # comment\n"
        "KEY6 =  spaced value  \n"
        "KEY7=\n"
        "not a line\n"
        "1BAD=x\n"
        "KEY8 = 'with = sign'\n"
    )
    assert parse_env_text(text) == {
        "KEY1": "value one", "KEY2": "v2", "KEY3": "v3", "KEY4": "quoted", "KEY5": "plain",
        "KEY6": "spaced value", "KEY7": "", "KEY8": "with = sign",
    }


def test_export_with_spaces_and_double_quotes():
    assert parse_env_text('export OPENROUTER_API_KEY = "sk-test"\n') == {"OPENROUTER_API_KEY": "sk-test"}


def test_load_env_does_not_override_and_returns_names_only(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text('A = "from-file"\nB=new\n', encoding="utf-8")
    environ = {"A": "existing"}
    added = load_env(env_file, environ=environ)
    assert added == ["B"]
    assert environ == {"A": "existing", "B": "new"}


def test_load_env_missing_file_is_fine(tmp_path):
    environ = {}
    assert load_env(tmp_path / "nope.env", environ=environ) == []
    assert environ == {}


def test_get_openrouter_key():
    assert get_openrouter_key({}) is None
    assert get_openrouter_key({"OPENROUTER_API_KEY": "   "}) is None
    assert get_openrouter_key({"OPENROUTER_API_KEY": "sk-x"}) == "sk-x"


def test_get_openrouter_key_from_process_env_absent():
    # the autouse fixture removes the variable
    assert get_openrouter_key() is None


def test_load_env_into_os_environ(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENROUTER_API_KEY = 'sk-file'\n", encoding="utf-8")
    load_env(env_file)
    assert get_openrouter_key() == "sk-file"

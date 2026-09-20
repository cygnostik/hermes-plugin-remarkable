"""Hermes registration; no network, installs, or state mutation on import."""
from functools import partial
from pathlib import Path

CONFIG_DEFAULTS = {
    'token_file': '', 'state_dir': '', 'node_path': 'node', 'timeout_ms': 180000,
    'transport': 'cloud', 'renderer_python': '', 'render_timeout': 180,
    'usb_enabled': False, 'usb_allow_upload': False, 'ssh_enabled': False,
    'usb_url': 'http://10.11.99.1', 'ssh_host': '', 'ssh_user': 'root',
    'ssh_port': 22, 'ssh_identity': '',
}


def register(ctx):
    from . import cli, schemas, tools
    # Bind immutable settings to each registration instead of sharing one global
    # config across profiles served by the same Hermes process.
    config = {key: ctx.get_config(key, default) for key, default in CONFIG_DEFAULTS.items()}
    for schema in schemas.ALL:
        name = schema['name']
        ctx.register_tool(name=name, toolset='remarkable', schema=schema,
                          handler=partial(getattr(tools, name), config=config))
    ctx.register_cli_command(name='remarkable', help='Pair and configure the reMarkable tablet plugin',
                             setup_fn=cli.setup_parser, handler_fn=partial(cli.run, config=config))
    skill = Path(__file__).parent / 'skills' / 'tablet' / 'SKILL.md'
    if skill.is_file():
        ctx.register_skill('tablet', skill)

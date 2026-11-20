update:
    uv lock --upgrade
    uv sync --locked
    prek autoupdate
    npx --yes actions-up --yes

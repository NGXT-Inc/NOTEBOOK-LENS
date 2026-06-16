# Notebook Lens

<p align="center">
  <img src="assets/notebook-lens-hero.png" alt="Notebook Lens connects an agent shell to a standard Jupyter notebook" width="100%">
</p>

<p align="center">
  <img alt="standard ipynb" src="https://img.shields.io/badge/artifact-standard%20.ipynb-0f766e?style=for-the-badge">
  <img alt="shell native" src="https://img.shields.io/badge/interface-shell%20native-f59e0b?style=for-the-badge">
  <img alt="agent loop" src="https://img.shields.io/badge/workflow-agent%20cell%20loop-2563eb?style=for-the-badge">
</p>

Notebook Lens is a shell-native control plane for agents to create, execute,
inspect, repair, and validate standard Jupyter notebooks without taking over the
human notebook UI.

The agent drives the notebook from the shell. The user opens the same `.ipynb`
in JupyterLab, classic Jupyter Notebook, or VS Code.

## Why

Use Notebook Lens when the final artifact should be a normal notebook, but the
work is being done by an agent:

- build research notebooks one cell at a time
- keep a persistent kernel across CLI calls
- inspect and repair failed or stale cells
- detect source/output divergence after human edits
- rerun from a fresh kernel with `run-clean` when the notebook needs to become evidence
- export rich outputs only when the agent explicitly asks for files

It is not a notebook UI, scheduler, reactive notebook system, or notebook diff
tool. It keeps the boring pieces: standard `.ipynb`, `ipykernel`, and Jupyter
viewers.

## Loop

```mermaid
flowchart LR
  add["add-code"] --> inspect["state"]
  inspect --> detail["show-cell"]
  detail --> repair{"failed or stale?"}
  repair -->|"yes"| update["update-code"]
  update --> inspect
  repair -->|"no"| clean["run-clean"]
  clean --> evidence["trusted notebook"]

  classDef command fill:#eff6ff,stroke:#2563eb,color:#172554;
  classDef check fill:#fff7ed,stroke:#f59e0b,color:#7c2d12;
  classDef done fill:#ecfdf5,stroke:#0f766e,color:#064e3b;
  class add,inspect,detail,update,clean command;
  class repair check;
  class evidence done;
```

## Quickstart

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .

export NL_EXPERIMENT_DIR="$PWD"
export NL_NOTEBOOK_DIR="$PWD/notebooks"
export NL_RUNTIME_DIR="$PWD/.notebook_lens"
export NL_ARTIFACT_DIR="$PWD/artifacts"

notebook-lens new notebooks/explore.ipynb
notebook-lens add-code notebooks/explore.ipynb --desc "Probe" --code 'print("hello")'
notebook-lens state notebooks/explore.ipynb --outputs summary
```

Open the notebook with your normal viewer:

```sh
jupyter lab notebooks/explore.ipynb
# or: jupyter notebook notebooks/explore.ipynb
# or: code notebooks/explore.ipynb
```

## Commands

```sh
notebook-lens add-code notebooks/explore.ipynb --file cell.py
notebook-lens update-code notebooks/explore.ipynb --id ab12cd34 --file fixed.py
notebook-lens add-markdown notebooks/explore.ipynb --file note.md
notebook-lens show-cell notebooks/explore.ipynb --id ab12cd34 --outputs full
notebook-lens export-output notebooks/explore.ipynb --id ab12cd34 --dir tmp/rich-output
notebook-lens run-clean notebooks/explore.ipynb
```

Use `--json` for structured agent output. Use `notebook-lens --help` for the
full command surface.

## Safety Rails

Notebook Lens stores execution metadata in cell metadata and uses it to warn
agents before they trust stale notebook state:

- external notebook edits reset trust in the live kernel
- code cells track the source hash that produced saved outputs
- downstream cells are marked stale after upstream edits
- failed cells block blind appends
- `run-clean` recomputes from a fresh kernel without deleting artifacts

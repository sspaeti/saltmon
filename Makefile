# Salt Fibre Box X6 network monitor
LOG ?= out/samples.jsonl
INTERVAL ?= 3

.PHONY: tui once log probe report duck names unblock help

tui:            ## start live TUI (default)
	uv run saltmon.py --interval $(INTERVAL)

log:            ## live TUI + append every poll to $(LOG) for later analysis
	uv run saltmon.py --interval $(INTERVAL) --log $(LOG)

once:           ## one table snapshot, no TUI
	uv run saltmon.py --once --interval 5 --log $(LOG)

probe:          ## dump raw router API subtrees to out/*.json
	uv run probe.py

report:         ## DuckDB aggregations over $(LOG)
	uv run report.py $(LOG)

duck:           ## open duckdb shell with views `samples`, `rows`, `summary` on $(LOG)
	duckdb -init sql/init.sql -cmd "SET VARIABLE log='$(LOG)';" -cmd ".read sql/views.sql"

names:          ## edit device names (MAC -> friendly name)
	$${EDITOR:-vi} names.json

help:
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed 's/:.*##/\t/' | expand -t 12

unblock:        ## lift every temporary Wi-Fi block held in out/blocks.json
	uv run saltmon.py --unblock-all

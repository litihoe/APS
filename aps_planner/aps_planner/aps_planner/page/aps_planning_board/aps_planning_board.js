frappe.pages["aps-planning-board"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("APS Planning Board"),
		single_column: true,
	});

	const board = new APSPlanningBoard(page);

	page.set_primary_action(__("Re-plan"), () => board.replan(), "refresh");
	page.add_button(__("Refresh"), () => board.load());

	board.load();
};

class APSPlanningBoard {
	constructor(page) {
		this.page = page;
		this.$body = $(`
			<div class="aps-board">
				<div class="aps-kpis row"></div>
				<div class="aps-legend text-muted small"></div>
				<div class="aps-timeline"></div>
			</div>
		`).appendTo(page.main);
		this._inject_styles();
	}

	// ---- data ------------------------------------------------------------ //
	load() {
		this.page.set_indicator(__("Loading…"), "orange");
		frappe.call({
			method: "aps_planner.api.get_schedule",
			callback: (r) => this.render(r.message || {}),
		});
	}

	replan() {
		const horizon_days = 7;
		frappe.confirm(
			__("Run a new optimisation over the next {0} days?", [horizon_days]),
			() => {
				this.page.set_indicator(__("Solving…"), "orange");
				frappe.call({
					method: "aps_planner.api.run_schedule",
					args: { horizon_days, max_solve_seconds: 60 },
					callback: (r) => this._poll(r.message.schedule_run),
				});
			}
		);
	}

	_poll(run_name) {
		const tick = () => {
			frappe.call({
				method: "aps_planner.api.get_run_status",
				args: { schedule_run: run_name },
				callback: (r) => {
					const status = (r.message || {}).status;
					if (["Completed", "Failed"].includes(status)) {
						if (status === "Failed") {
							frappe.msgprint({
								title: __("Solve failed"),
								message: __("See the Schedule Run error log."),
								indicator: "red",
							});
						}
						this.load();
					} else {
						setTimeout(tick, 2000);
					}
				},
			});
		};
		setTimeout(tick, 2000);
	}

	// ---- render ---------------------------------------------------------- //
	render(data) {
		const ops = data.operations || [];
		this._render_kpis(data);
		if (!ops.length) {
			this.page.set_indicator(__("No schedule"), "gray");
			this.$body.find(".aps-timeline").html(
				`<div class="text-muted text-center padding">${__(
					"No schedule yet — press Re-plan to generate one."
				)}</div>`
			);
			return;
		}
		const colour = this._job_colours(ops);
		this._render_legend(colour);
		this._render_timeline(ops, colour);
		this.page.set_indicator(
			`${data.status || ""} · ${data.solver_status || ""}`,
			data.status === "Completed" ? "green" : "orange"
		);
	}

	_render_kpis(data) {
		const k = data.kpis || {};
		const ontime =
			k.total_jobs > 0
				? Math.round((100 * (k.on_time_jobs || 0)) / k.total_jobs)
				: 0;
		const cards = [
			[__("On-time"), `${ontime}%`, `${k.on_time_jobs || 0}/${k.total_jobs || 0} ${__("jobs")}`],
			[__("Weighted tardiness"), this._fmt_dur(k.weighted_tardiness), __("priority-weighted late minutes")],
			[__("Makespan"), this._fmt_dur(k.makespan_minutes), __("plan span")],
			[__("Reassigned"), k.reassigned_ops || 0, __("ops moved (last re-solve)")],
		];
		this.$body.find(".aps-kpis").html(
			cards
				.map(
					([label, value, sub]) => `
				<div class="col-sm-3">
					<div class="aps-kpi">
						<div class="aps-kpi-value">${value}</div>
						<div class="aps-kpi-label">${label}</div>
						<div class="aps-kpi-sub text-muted">${sub}</div>
					</div>
				</div>`
				)
				.join("")
		);
	}

	_render_legend(colour) {
		const items = Object.keys(colour)
			.map(
				(job) =>
					`<span class="aps-legend-item"><span class="aps-swatch" style="background:${colour[job]}"></span>${frappe.utils.escape_html(
						job
					)}</span>`
			)
			.join(" ");
		this.$body
			.find(".aps-legend")
			.html(`${__("Jobs")}: ${items} &nbsp;·&nbsp; ${__("click a bar to pin/unpin")}`);
	}

	_render_timeline(ops, colour) {
		// Group by workstation; rows = machines, x = time.
		const rows = {};
		let t0 = Infinity;
		let t1 = -Infinity;
		ops.forEach((o) => {
			const ws = o.workstation || __("Unassigned");
			(rows[ws] = rows[ws] || []).push(o);
			t0 = Math.min(t0, this._ms(o.planned_start));
			t1 = Math.max(t1, this._ms(o.planned_end));
		});
		const span = Math.max(t1 - t0, 1);

		const $tl = this.$body.find(".aps-timeline").empty();
		$tl.append(this._axis(t0, t1));

		Object.keys(rows)
			.sort()
			.forEach((ws) => {
				const $row = $(
					`<div class="aps-row"><div class="aps-row-label">${frappe.utils.escape_html(
						ws
					)}</div><div class="aps-track"></div></div>`
				);
				const $track = $row.find(".aps-track");
				rows[ws].forEach((o) => {
					const left = (100 * (this._ms(o.planned_start) - t0)) / span;
					const width = Math.max(
						(100 * (this._ms(o.planned_end) - this._ms(o.planned_start))) / span,
						0.4
					);
					const setupW =
						(100 * (this._ms(o.setup_end) - this._ms(o.planned_start))) / span;
					const $bar = $(`
						<div class="aps-bar ${o.pinned ? "pinned" : ""}"
							 style="left:${left}%;width:${width}%;background:${colour[o.work_order]}">
							<div class="aps-setup" style="width:${Math.max(
								(100 * setupW) / Math.max(width, 0.0001),
								0
							)}%"></div>
							<span class="aps-bar-label">${frappe.utils.escape_html(o.work_order)}</span>
						</div>
					`);
					$bar.attr(
						"title",
						`${o.work_order} · ${o.operation || ""}\n` +
							`${this._fmt_dt(o.planned_start)} → ${this._fmt_dt(o.planned_end)}\n` +
							`${__("Operator")}: ${o.operator || "—"} · ${__("Tool")}: ${o.tool || "—"}` +
							(o.pinned ? `\n📌 ${__("pinned")}` : "")
					);
					$bar.on("click", () => this._toggle_pin(o, $bar));
					$track.append($bar);
				});
				$tl.append($row);
			});
	}

	_toggle_pin(op, $bar) {
		const next = op.pinned ? 0 : 1;
		frappe.call({
			method: "aps_planner.api.pin_operation",
			args: { name: op.name, pinned: next },
			callback: () => {
				op.pinned = next;
				$bar.toggleClass("pinned", !!next);
				frappe.show_alert({
					message: next ? __("Pinned") : __("Unpinned"),
					indicator: next ? "blue" : "gray",
				});
			},
		});
	}

	_axis(t0, t1) {
		const $axis = $(`<div class="aps-row aps-axis"><div class="aps-row-label"></div><div class="aps-track"></div></div>`);
		const $track = $axis.find(".aps-track");
		const span = Math.max(t1 - t0, 1);
		const ticks = 6;
		for (let i = 0; i <= ticks; i++) {
			const at = t0 + (span * i) / ticks;
			$track.append(
				`<div class="aps-tick" style="left:${(100 * i) / ticks}%">${this._fmt_dt(
					new Date(at).toISOString()
				)}</div>`
			);
		}
		return $axis;
	}

	// ---- helpers --------------------------------------------------------- //
	_ms(dt) {
		return frappe.datetime.str_to_obj(dt).getTime();
	}

	_fmt_dt(dt) {
		return frappe.datetime.str_to_user(
			typeof dt === "string" ? dt : frappe.datetime.obj_to_str(dt)
		);
	}

	_fmt_dur(mins) {
		if (!mins) return "0";
		const h = Math.floor(mins / 60);
		const m = Math.round(mins % 60);
		return h ? `${h}h ${m}m` : `${m}m`;
	}

	_job_colours(ops) {
		const palette = [
			"#5e64ff", "#28a745", "#ff5858", "#ffa00a", "#00b8d4",
			"#7b2fbe", "#e91e63", "#795548", "#607d8b", "#009688",
		];
		const colour = {};
		let i = 0;
		ops.forEach((o) => {
			if (!(o.work_order in colour)) {
				colour[o.work_order] = palette[i % palette.length];
				i++;
			}
		});
		return colour;
	}

	_inject_styles() {
		if (document.getElementById("aps-board-styles")) return;
		$(`<style id="aps-board-styles">
			.aps-board { padding: 4px 2px 24px; }
			.aps-kpis { margin-bottom: 12px; }
			.aps-kpi { background: var(--card-bg, #fff); border: 1px solid var(--border-color, #e2e6e9);
				border-radius: 8px; padding: 12px 14px; }
			.aps-kpi-value { font-size: 22px; font-weight: 700; line-height: 1.1; }
			.aps-kpi-label { font-size: 12px; font-weight: 600; margin-top: 2px; }
			.aps-kpi-sub { font-size: 11px; }
			.aps-legend { margin: 4px 2px 10px; }
			.aps-legend-item { margin-right: 10px; white-space: nowrap; }
			.aps-swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px;
				margin-right: 4px; vertical-align: middle; }
			.aps-timeline { border: 1px solid var(--border-color, #e2e6e9); border-radius: 8px;
				overflow: hidden; }
			.aps-row { display: flex; align-items: stretch; border-top: 1px solid var(--border-color, #eef0f2); }
			.aps-row:first-child { border-top: 0; }
			.aps-row-label { flex: 0 0 130px; padding: 8px 10px; font-size: 12px; font-weight: 600;
				background: var(--subtle-fg, #f7f9fa); border-right: 1px solid var(--border-color, #e2e6e9);
				display: flex; align-items: center; }
			.aps-track { position: relative; flex: 1 1 auto; min-height: 34px; }
			.aps-axis .aps-track { min-height: 22px; }
			.aps-tick { position: absolute; top: 4px; font-size: 10px; color: var(--text-muted, #8d99a6);
				transform: translateX(-50%); white-space: nowrap; }
			.aps-bar { position: absolute; top: 5px; bottom: 5px; border-radius: 4px; color: #fff;
				font-size: 11px; cursor: pointer; overflow: hidden; box-shadow: inset 0 0 0 1px rgba(0,0,0,.08);
				display: flex; align-items: center; }
			.aps-bar.pinned { box-shadow: 0 0 0 2px #1f2937, inset 0 0 0 1px rgba(255,255,255,.4); }
			.aps-setup { position: absolute; left: 0; top: 0; bottom: 0;
				background: repeating-linear-gradient(45deg, rgba(255,255,255,.35),
					rgba(255,255,255,.35) 4px, transparent 4px, transparent 8px); }
			.aps-bar-label { position: relative; padding: 0 6px; white-space: nowrap;
				text-shadow: 0 1px 1px rgba(0,0,0,.3); }
		</style>`).appendTo(document.head);
	}
}

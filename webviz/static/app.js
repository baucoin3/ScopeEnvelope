// Consumes the WireEvent contract (webviz/events.py) over SSE and drives
// two live force-graphs + a shared trend chart. Keys here are copied
// verbatim from that module -- condition, type, step, tool, path, reason,
// in_scope, prompt_tokens_cum, cost_usd_cum, tool_calls_cum.

const COLORS = {
  read: "#4da3ff",
  written: "#4caf50",
  blocked: "#e74c3c",
};

const CONDITIONS = ["lazy", "envelope"];

class ConditionGraph {
  constructor(condition, containerEl) {
    this.condition = condition;
    this.nodes = new Map(); // path -> node
    this.links = []; // {source, target}
    this.lastGoodId = null;
    this.filesRead = new Set();

    this.containerEl = containerEl;
    this.graph = ForceGraph()(containerEl)
      // force-graph does not reliably auto-size from the container (it
      // fell back to the full window's dimensions in testing, clipped only
      // by the container's overflow:hidden) -- set explicitly from the
      // container's own measured size instead of trusting the default.
      .width(containerEl.clientWidth)
      .height(containerEl.clientHeight)
      .nodeId("id")
      .nodeLabel((n) => n.id)
      .nodeRelSize(5)
      .linkDirectionalArrowLength(4)
      .linkDirectionalArrowRelPos(1)
      .linkColor(() => "rgba(255,255,255,0.3)")
      .nodeCanvasObject((node, ctx, globalScale) => this._drawNode(node, ctx, globalScale))
      .nodePointerAreaPaint((node, color, ctx) => {
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(node.x, node.y, 8, 0, 2 * Math.PI, false);
        ctx.fill();
      })
      .cooldownTicks(Infinity);
    this.graph.graphData({ nodes: [], links: [] });

    window.addEventListener("resize", () => {
      this.graph.width(this.containerEl.clientWidth).height(this.containerEl.clientHeight);
    });
  }

  _drawNode(node, ctx, globalScale) {
    const flashing = node.flashUntil && Date.now() < node.flashUntil;
    const baseR = 6;
    const r = flashing ? baseR + 3 : baseR;

    if (flashing) {
      ctx.beginPath();
      ctx.arc(node.x, node.y, r + 4, 0, 2 * Math.PI, false);
      ctx.fillStyle = (COLORS[node.status] || "#999") + "55";
      ctx.fill();
    }

    ctx.beginPath();
    ctx.arc(node.x, node.y, r, 0, 2 * Math.PI, false);
    ctx.fillStyle = COLORS[node.status] || "#999";
    ctx.fill();

    if (node.inScope) {
      ctx.setLineDash([2, 2]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = "#f2f2f2";
      ctx.stroke();
      ctx.setLineDash([]);
    }

    const label = node.id.split("/").pop();
    const fontSize = 10 / globalScale;
    ctx.font = `${fontSize}px sans-serif`;
    ctx.fillStyle = "#cfd3da";
    ctx.textAlign = "center";
    ctx.fillText(label, node.x, node.y + r + fontSize + 1);
  }

  _ensureNode(path, status, inScope) {
    let node = this.nodes.get(path);
    if (!node) {
      node = { id: path, status, inScope: !!inScope };
      this.nodes.set(path, node);
    } else {
      // write/blocked override read; don't let a later read downgrade
      // a node that was already written (mirrors state.py's precedence).
      if (status === "written" || status === "blocked") node.status = status;
      if (inScope) node.inScope = true;
    }
    return node;
  }

  _refresh() {
    this.graph.graphData({
      nodes: Array.from(this.nodes.values()),
      links: this.links.slice(),
    });
    // force-graph doesn't auto-fit the camera to the simulation -- without
    // this, repulsion between a handful of sparsely-connected nodes can
    // drift them past the visible canvas edge long before the layout
    // settles, especially early on when there are few links to hold
    // things together.
    this.graph.zoomToFit(400, 30);
  }

  handleEvent(evt) {
    if (evt.type === "done") {
      // Don't cooldownTicks(0) here -- that stops the simulation's render
      // loop immediately, which can freeze the canvas on a stale frame
      // painted *before* the last node/link update if "done" arrives
      // faster than the next animation frame. Letting it keep running is
      // visually harmless (a settled layout barely moves) and guarantees
      // the final state actually gets painted.
      this._refresh();
      return;
    }

    if (!evt.path) {
      // e.g. run_tests -- no node to draw, counters still update elsewhere.
      return;
    }

    if (evt.tool === "read_file") this.filesRead.add(evt.path);

    if (evt.type === "blocked") {
      const node = this._ensureNode(evt.path, "blocked", evt.in_scope);
      node.flashUntil = Date.now() + 900;

      if (this.lastGoodId && this.lastGoodId !== evt.path) {
        const link = { source: this.lastGoodId, target: evt.path };
        this.links.push(link);
        this._refresh();
        setTimeout(() => {
          this.links = this.links.filter((l) => l !== link);
          this._refresh();
        }, 900);
      } else {
        this._refresh();
      }
      return; // blocked calls never become the new "last good" node
    }

    // type === "call"
    const status = evt.tool === "read_file" ? "read" : evt.tool === "write_file" ? "written" : null;
    if (!status) return;

    const node = this._ensureNode(evt.path, status, evt.in_scope);
    node.flashUntil = Date.now() + 500;

    if (this.lastGoodId && this.lastGoodId !== evt.path) {
      this.links.push({ source: this.lastGoodId, target: evt.path });
    }
    this.lastGoodId = evt.path;
    this._refresh();
  }
}

class Dashboard {
  constructor() {
    this.graphs = {};
    this.trendData = { lazy: [], envelope: [] };
    this.metric = "tokens";

    for (const condition of CONDITIONS) {
      this.graphs[condition] = new ConditionGraph(condition, document.getElementById(`graph-${condition}`));
    }

    this.chart = new Chart(document.getElementById("trend-chart"), {
      type: "line",
      data: {
        datasets: [
          this._dataset("lazy", "#ff8a65"),
          this._dataset("envelope", "#4db6ac"),
        ],
      },
      options: {
        responsive: true,
        animation: false,
        parsing: false,
        scales: {
          x: { type: "linear", title: { display: true, text: "step" }, ticks: { color: "#aab0bb" } },
          y: { title: { display: true, text: "prompt tokens (cumulative)" }, ticks: { color: "#aab0bb" } },
        },
        plugins: { legend: { labels: { color: "#e6e6e6" } } },
      },
    });

    document.querySelectorAll('input[name="metric"]').forEach((input) => {
      input.addEventListener("change", (e) => this._setMetric(e.target.value));
    });

    this.source = new EventSource("/events");
    this.source.onmessage = (e) => this.handleMessage(JSON.parse(e.data));
  }

  _dataset(condition, color) {
    return { label: condition, borderColor: color, backgroundColor: color, data: [], tension: 0.15, pointRadius: 2 };
  }

  _setMetric(metric) {
    this.metric = metric;
    this.chart.options.scales.y.title.text =
      metric === "tokens" ? "prompt tokens (cumulative)" : "cost $ (cumulative)";
    this.chart.data.datasets.forEach((ds) => {
      ds.data = this.trendData[ds.label].map((p) => ({ x: p.step, y: metric === "tokens" ? p.tokens : p.cost }));
    });
    this.chart.update();
  }

  _pushTrendPoint(condition, evt) {
    this.trendData[condition].push({
      step: evt.step,
      tokens: evt.prompt_tokens_cum,
      cost: evt.cost_usd_cum,
    });
    const ds = this.chart.data.datasets.find((d) => d.label === condition);
    ds.data.push({ x: evt.step, y: this.metric === "tokens" ? evt.prompt_tokens_cum : evt.cost_usd_cum });
    this.chart.update();
  }

  _updateCounters(condition, evt) {
    document.getElementById(`calls-${condition}`).textContent = evt.tool_calls_cum;
    document.getElementById(`reads-${condition}`).textContent = this.graphs[condition].filesRead.size;
    document.getElementById(`tokens-${condition}`).textContent =
      evt.prompt_tokens_cum != null ? evt.prompt_tokens_cum.toLocaleString() : "--";
    document.getElementById(`cost-${condition}`).textContent =
      evt.cost_usd_cum != null ? `$${evt.cost_usd_cum.toFixed(5)}` : "--";
  }

  handleMessage(evt) {
    const { condition } = evt;
    const graph = this.graphs[condition];
    if (!graph) return;

    graph.handleEvent(evt);
    this._updateCounters(condition, evt);

    if (evt.type !== "done") {
      this._pushTrendPoint(condition, evt);
    } else {
      const statusEl = document.getElementById(`status-${condition}`);
      statusEl.textContent = "done";
      statusEl.classList.add("done");
    }
  }
}

window.addEventListener("DOMContentLoaded", () => {
  window.dashboard = new Dashboard();
});

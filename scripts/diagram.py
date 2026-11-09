"""Draw the architecture diagram (results/figures/architecture.png)."""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "results" / "figures"
OUT.mkdir(parents=True, exist_ok=True)

INK, INK2 = "#0b0b0b", "#52514e"
FILL = {"edge": "#e8f0fb", "svc": "#eef6f1", "data": "#fdf1e6", "obs": "#f1effb", "opt": "#f4f4f2"}
LINE = {"edge": "#2a78d6", "svc": "#1baf7a", "data": "#eb6834", "obs": "#6250d6", "opt": "#8a8984"}

fig, ax = plt.subplots(figsize=(11, 6.6))
ax.set_xlim(0, 110)
ax.set_ylim(-4, 70)
ax.axis("off")


def box(x, y, w, h, title, sub="", kind="svc", dashed=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.3,rounding_size=1.2", fc=FILL[kind],
                                ec=LINE[kind], lw=1.4, ls="--" if dashed else "-"))
    ax.text(x + w / 2, y + h - 1.6, title, ha="center", va="top", fontsize=9, color=INK, weight="bold")
    if sub:
        ax.text(x + w / 2, y + h - 4.2, sub, ha="center", va="top", fontsize=7, color=INK2, linespacing=1.3)


def arrow(x1, y1, x2, y2, text="", color=INK2, style="-|>", ls="-", rad=0.0):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style, mutation_scale=10, color=color, lw=1.1,
                                 ls=ls, connectionstyle=f"arc3,rad={rad}"))
    if text:
        ax.text((x1 + x2) / 2, (y1 + y2) / 2 + 0.8, text, ha="center", fontsize=6.5, color=INK2)


# client and gateway
box(2, 52, 14, 9, "Clients", "students, registrar,\nadmin (synthetic)", "opt")
box(24, 49, 24, 14, "API Gateway (nginx)",
    "load balancing + passive health checks\ntimeouts, bounded failover (GET only)\n"
    "rate limiting, 16 KB body limit\nX-Request-ID, JSON access logs\n:80 public  |  :8081 internal", "edge")
arrow(16, 56.5, 24, 56.5, "HTTPS/JSON")

# services
svc = [("student-service", "auth (JWT), profiles,\ndashboard aggregation", "x2", 4),
       ("payment-service", "tuition payments,\nidempotency, atomic txn", "x2", 31),
       ("records-service", "grades, transcripts,\nprofile cache fallback", "x2", 58),
       ("timetable-service", "schedule (non-critical,\ngraceful degradation)", "x1", 85)]
for name, sub, rep, x in svc:
    extra = "\nreplica read fallback" if name.startswith("timetable") else "\ntimeouts, retries, circuit breakers"
    box(x, 28, 22, 13, f"{name}  [{rep}]", sub + extra, "svc")
    arrow(36, 49, x + 11, 41.3, rad=0.0)
ax.text(55, 45.5, "round-robin to replicas", fontsize=6.5, color=INK2, ha="left")
arrow(15, 41.3, 30, 49, "", LINE["svc"], ls=":", rad=-0.25)
ax.text(5, 45, "service-to-service calls\nvia :8081 + service token", fontsize=6.5, color=INK2)

# data tier
box(18, 6, 24, 13, "PostgreSQL primary", "reads + writes, WAL streaming\nleast-privilege role per service\n"
    "restart policy (self-healing)", "data")
box(50, 6, 22, 13, "PostgreSQL replica", "hot standby (read-only)\nread failover target", "data")
box(80, 6, 22, 13, "Backup job", "pg_dump every 5 min\nkeeps last 6 dumps", "data")
for x in (15, 42, 69):
    arrow(x, 28, 30, 19.3, rad=0.0)
arrow(42.3, 12.5, 49.7, 12.5, "streaming replication", LINE["data"])
arrow(91, 5.7, 30, 5.7, "", LINE["data"], ls="--", rad=-0.12)
ax.text(61, -1.6, "logical backup (pg_dump)", fontsize=6.5, color=INK2, ha="center")
arrow(46, 28, 61, 19.3, "read failover", LINE["data"], ls="--", rad=0.1)

# observability
box(84, 49, 23, 14, "Prometheus + Grafana",
    "scrape /metrics every 2 s\n6 alert rules, dashboard\nlogs: docker JSON stdout", "obs")
arrow(96, 49, 96, 41.3, "scrape", LINE["obs"], style="<|-")

ax.text(1, 66.5, "Baseline (midterm) configuration: one instance per service, no replica/backup, no restart policy, "
        "shared DB owner account,\nno timeouts/retries/circuit breakers/idempotency, no authentication, no rate limiting.",
        fontsize=7, color=INK2)
fig.tight_layout()
fig.savefig(OUT / "architecture.png", dpi=170)
print("saved", OUT / "architecture.png")

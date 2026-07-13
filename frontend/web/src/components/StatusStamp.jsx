import { STATUS_META } from "../statusLogic";

export default function StatusStamp({ status, delay = 0 }) {
  const meta = STATUS_META[status] || STATUS_META.pending;
  const isStamped = status === "passed" || status === "failed" || status === "halted";

  return (
    <span
      className="status-stamp"
      style={{
        "--stamp-color": meta.color,
        animationDelay: isStamped ? `${delay}ms` : undefined,
      }}
      data-stamped={isStamped}
    >
      {meta.label}
      <style>{`
        .status-stamp {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          font-family: var(--mono);
          font-size: 11px;
          letter-spacing: 0.12em;
          text-transform: uppercase;
          padding: 4px 10px;
          border: 1.5px solid var(--stamp-color);
          border-radius: 3px;
          color: var(--stamp-color);
          white-space: nowrap;
        }
        .status-stamp[data-stamped="true"] {
          animation: stampIn 0.5s cubic-bezier(0.2, 0.8, 0.3, 1.2) both;
        }
        .status-stamp::before {
          content: "";
          width: 6px;
          height: 6px;
          border-radius: 50%;
          background: var(--stamp-color);
        }
      `}</style>
    </span>
  );
}

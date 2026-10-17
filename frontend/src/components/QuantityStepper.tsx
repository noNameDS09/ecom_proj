interface QuantityStepperProps {
  value: number;
  min?: number;
  max: number;
  onChange: (value: number) => void;
  disabled?: boolean;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

export default function QuantityStepper({ value, min = 1, max, onChange, disabled = false }: QuantityStepperProps) {
  return (
    <div className="stepper">
      <button
        type="button"
        className="stepper-btn"
        onClick={() => onChange(clamp(value - 1, min, max))}
        disabled={disabled || value <= min}
        aria-label="Decrease quantity"
      >
        −
      </button>
      <input
        className="stepper-input"
        type="number"
        inputMode="numeric"
        min={min}
        max={max}
        value={value}
        disabled={disabled}
        aria-label="Quantity"
        onChange={(event) => {
          const next = Number(event.target.value);
          if (Number.isFinite(next)) onChange(clamp(Math.trunc(next), min, max));
        }}
      />
      <button
        type="button"
        className="stepper-btn"
        onClick={() => onChange(clamp(value + 1, min, max))}
        disabled={disabled || value >= max}
        aria-label="Increase quantity"
      >
        +
      </button>
    </div>
  );
}

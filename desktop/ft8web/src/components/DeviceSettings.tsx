import { useEffect, useRef, useState } from "react";

export interface DeviceForm {
	rig_model: number;
	rig_device: string;
	rig_baud: number;
	rigctld_port: number;
	audio_device: string | null;
}

export const RIG_MODEL_OPTIONS = [
	{ model: 1020, name: "Yaesu FT-817" },
	{ model: 1049, name: "Yaesu FT-710" },
	{ model: 3073, name: "Icom IC-7300" },
	{ model: 30003, name: "Icom IC-M710" },
];
export const BAUD_OPTIONS = [
	1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200,
];

export function formFromConfig(
	cfg: Record<string, any> | undefined,
): DeviceForm {
	return {
		rig_model: typeof cfg?.rig_model === "number" ? cfg.rig_model : 1049,
		rig_device: typeof cfg?.rig_device === "string" ? cfg.rig_device : "",
		rig_baud: typeof cfg?.rig_baud === "number" ? cfg.rig_baud : 38400,
		rigctld_port:
			typeof cfg?.rigctld_port === "number" ? cfg.rigctld_port : 4532,
		audio_device: cfg?.audio_device ?? null,
	};
}

export function isCustomModel(form: DeviceForm): boolean {
	return !RIG_MODEL_OPTIONS.some((o) => o.model === form.rig_model);
}

export function sourceLabel(
	source: Record<string, string> | undefined,
	key: string,
): string {
	return source?.[key] ?? "default";
}

export interface DeviceSettingsProps {
	config: Record<string, any> | undefined;
	source: Record<string, string> | undefined;
	audioDevices: Array<{
		index: number;
		name: string;
		max_input: number;
		max_output: number;
	}>;
	serialDevices: string[];
	busy: boolean;
	onSave: (form: DeviceForm) => Promise<string | null>; // error message, null = ok
	onApply: () => Promise<string | null>;
}

export function DeviceSettings(props: DeviceSettingsProps) {
	const { config, source, audioDevices, serialDevices, busy, onSave, onApply } =
		props;
	const [form, setForm] = useState<DeviceForm>(() => formFromConfig(config));
	const [customModel, setCustomModel] = useState(() => String(form.rig_model));
	const [modelIsCustom, setModelIsCustom] = useState(() => isCustomModel(form));
	const [customDevice, setCustomDevice] = useState("");
	const [deviceIsCustom, setDeviceIsCustom] = useState(
		() => !!(form.rig_device && !serialDevices.includes(form.rig_device)),
	);
	const [error, setError] = useState<string | null>(null);
	const [saved, setSaved] = useState(false);
	const [restarting, setRestarting] = useState(false);
	// The form last saved to the server: a config refresh triggered by the save
	// itself must not reset `saved` (otherwise Apply is permanently unreachable).
	const savedFormRef = useRef<DeviceForm | null>(null);

	useEffect(() => {
		const next = formFromConfig(config);
		setForm(next);
		setCustomModel(String(next.rig_model));
		setModelIsCustom(isCustomModel(next));
		setDeviceIsCustom(!!(next.rig_device && !serialDevices.includes(next.rig_device)));
		const s = savedFormRef.current;
		setSaved(Boolean(s) && JSON.stringify(s) === JSON.stringify(next));
	}, [config]);

	const effectiveCustomDevice = deviceIsCustom ? customDevice : "";

	const set = (patch: Partial<DeviceForm>) =>
		setForm((f) => ({ ...f, ...patch }));

	const handleSave = async () => {
		setError(null);
		const next: DeviceForm = { ...form };
		if (modelIsCustom) {
			const n = Number(customModel);
			if (!Number.isInteger(n) || n <= 0) {
				setError("Custom rig model must be a positive integer");
				return;
			}
			next.rig_model = n;
		}
		if (deviceIsCustom) {
			if (!effectiveCustomDevice.trim().startsWith("/dev/")) {
				setError("Custom serial device must be an absolute /dev/... path");
				return;
			}
			next.rig_device = effectiveCustomDevice;
		}
		const err = await onSave(next);
		if (err) setError(err);
		else {
			savedFormRef.current = next;
			setSaved(true);
		}
	};

	const handleApply = async () => {
		if (!saved) {
			setError("Save the device settings before applying");
			return;
		}
		if (
			!window.confirm(
				"Applying will restart rigctld and the server.\n" +
					"Connection drops for about 20 seconds and you must log in again.\nContinue?",
			)
		)
			return;
		setError(null);
		setRestarting(true);
		const err = await onApply();
		if (err) {
			setError(err);
			setRestarting(false);
		}
	};

	const selectCls =
		"bg-app border border-border-input text-text-main rounded px-3 py-2 text-xs font-mono w-full focus:outline-none focus:border-[#4caf50]";

	return (
		<div className="flex flex-col gap-3 pt-2 border-t border-border-subtle">
			<label className="text-[10px] uppercase tracking-widest text-text-muted">
				Devices
			</label>

			<div className="flex flex-col gap-1">
				<label className="text-[10px] text-text-muted">
					Rig Model (hamlib)
				</label>
				<select
					className={selectCls}
					value={modelIsCustom ? "custom" : form.rig_model}
					onChange={(e) => {
						if (e.target.value === "custom") {
							setModelIsCustom(true);
							setCustomModel(String(form.rig_model));
							return;
						}
						setModelIsCustom(false);
						set({ rig_model: Number(e.target.value) });
					}}
					disabled={busy}
				>
					{RIG_MODEL_OPTIONS.map((o) => (
						<option key={o.model} value={o.model}>
							{o.name} ({o.model})
						</option>
					))}
					<option value="custom">Custom…</option>
				</select>
				{modelIsCustom && (
					<input
						type="number"
						min={1}
						className={selectCls}
						value={customModel}
						onChange={(e) => setCustomModel(e.target.value)}
					/>
				)}
			</div>

			<div className="flex flex-col gap-1">
				<label className="text-[10px] text-text-muted">CAT Serial Device</label>
				<select
					className={selectCls}
					value={deviceIsCustom ? "custom" : form.rig_device}
					onChange={(e) => {
						if (e.target.value === "custom") {
							setDeviceIsCustom(true);
							setCustomDevice(form.rig_device);
							return;
						}
						setDeviceIsCustom(false);
						set({ rig_device: e.target.value });
					}}
					disabled={busy}
				>
					<option value="">—</option>
					{serialDevices.map((d) => (
						<option key={d} value={d}>
							{d}
						</option>
					))}
					<option value="custom">Custom…</option>
				</select>
				{deviceIsCustom && (
					<input
						className={selectCls}
						value={effectiveCustomDevice}
						onChange={(e) => {
							setCustomDevice(e.target.value);
							set({ rig_device: e.target.value });
						}}
					/>
				)}
			</div>

			<div className="flex flex-col gap-1">
				<label className="text-[10px] text-text-muted">Baud Rate</label>
				<select
					className={selectCls}
					value={form.rig_baud}
					disabled={busy}
					onChange={(e) => set({ rig_baud: Number(e.target.value) })}
				>
					{BAUD_OPTIONS.map((b) => (
						<option key={b} value={b}>
							{b}
						</option>
					))}
				</select>
			</div>

			<div className="flex flex-col gap-1">
				<label className="text-[10px] text-text-muted">rigctld Port</label>
				<input
					type="number"
					min={1024}
					max={65535}
					className={selectCls}
					value={form.rigctld_port}
					disabled={busy}
					onChange={(e) => set({ rigctld_port: Number(e.target.value) })}
				/>
			</div>

			<div className="flex flex-col gap-1">
				<label className="text-[10px] text-text-muted">Audio Device</label>
				<select
					className={selectCls}
					value={form.audio_device ?? ""}
					disabled={busy}
					onChange={(e) => set({ audio_device: e.target.value || null })}
				>
					<option value="">System default</option>
					{audioDevices.map((d) => (
						<option key={d.index} value={d.name}>
							{d.name}
						</option>
					))}
				</select>
			</div>

			<p className="text-[9px] text-text-muted">
				Source — model: {sourceLabel(source, "rig_model")} · serial:{" "}
				{sourceLabel(source, "rig_device")} · baud:{" "}
				{sourceLabel(source, "rig_baud")} · port:{" "}
				{sourceLabel(source, "rigctld_port")} · audio:{" "}
				{sourceLabel(source, "audio_device")}
			</p>

			{restarting && (
				<p className="text-xs text-amber-400" role="status">
					Restarting… server will disconnect; log in again after it returns.
				</p>
			)}
			{error && (
				<p className="text-xs text-red-400" role="alert">
					{error}
				</p>
			)}

			<div className="flex gap-2 mt-1">
				<button
					onClick={() => void handleSave()}
					disabled={busy}
					className="bg-app border border-border-input text-text-main rounded px-3 py-1 text-xs font-mono hover:border-[#4caf50]"
				>
					Save
				</button>
				<button
					onClick={() => void handleApply()}
					disabled={busy}
					className="bg-green-600 hover:bg-green-600 text-black px-3 py-1 rounded text-xs font-bold uppercase tracking-widest"
				>
					Apply & Restart
				</button>
			</div>
		</div>
	);
}

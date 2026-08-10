import { describe, expect, it } from 'vitest';
import { BAUD_OPTIONS, RIG_MODEL_OPTIONS, formFromConfig, isCustomModel, sourceLabel } from './DeviceSettings';

describe('DeviceSettings helpers', () => {
  it('offers the curated rig models with hamlib numbers', () => {
    expect(RIG_MODEL_OPTIONS.map(o => o.model)).toEqual([1020, 1049, 3073, 30003]);
  });
  it('defaults a missing config to FT-710 / 38400 / port 4532', () => {
    const f = formFromConfig(undefined);
    expect(f.rig_model).toBe(1049);
    expect(f.rig_baud).toBe(38400);
    expect(f.rig_stop_bits).toBe(1);
    expect(f.rigctld_port).toBe(4532);
    expect(f.audio_in_device).toBeNull();
    expect(f.audio_out_device).toBeNull();
  });
  it('preserves the saved config', () => {
    const f = formFromConfig({
      rig_model: 3073,
      audio_in_device: 'USB In',
      audio_out_device: 'USB Out',
      rig_stop_bits: 2,
    });
    expect(f.rig_model).toBe(3073);
    expect(f.rig_stop_bits).toBe(2);
    expect(f.audio_in_device).toBe('USB In');
    expect(f.audio_out_device).toBe('USB Out');
  });
  it('falls back to the legacy single audio_device', () => {
    const f = formFromConfig({ audio_device: 'FT8' });
    expect(f.audio_in_device).toBe('FT8');
    expect(f.audio_out_device).toBe('FT8');
  });
  it('detects custom models', () => {
    expect(isCustomModel(formFromConfig({ rig_model: 9999 }))).toBe(true);
    expect(isCustomModel(formFromConfig({ rig_model: 1049 }))).toBe(false);
  });
  it('labels sources with a default', () => {
    expect(sourceLabel({ rig_model: 'file' }, 'rig_model')).toBe('file');
    expect(sourceLabel(undefined, 'audio_device')).toBe('default');
  });
  it('exposes common baud rates including the FT-710 value', () => {
    expect(BAUD_OPTIONS).toContain(38400);
    expect(BAUD_OPTIONS).toContain(115200);
  });
});

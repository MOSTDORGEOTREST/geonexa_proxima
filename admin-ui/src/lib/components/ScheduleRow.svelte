<script lang="ts">
	/**
	 * Строка расписания в таблице этапа плюс раскрывающийся редактор под ней.
	 *
	 * Всё, что нужно каждый день, — в одной строке: когда, с чем, включено ли,
	 * запустить. Редактор (период, пояс, параметры, разовый запуск) открывается
	 * шестерёнкой и не занимает места, пока не нужен.
	 */
	import { once } from '$lib/once';
	import ParamFields, { type FieldSpec } from '$lib/components/ParamFields.svelte';
	import { when } from '$lib/charts/format';

	let { schedule, form, canRun }: { schedule: any; form: any; canRun: boolean } = $props();

	const fields = $derived((schedule.fields ?? []) as FieldSpec[]);
	const parameters = $derived((schedule.parameters ?? {}) as Record<string, unknown>);
	const mine = $derived(form?.id === schedule.id);
	// svelte-ignore state_referenced_locally
	let open = $state(Boolean(form?.id === schedule.id && (form?.error || form?.saved)));
	// svelte-ignore state_referenced_locally
	let mode = $state<'cron' | 'interval'>(schedule.cron ? 'cron' : 'interval');

	const UNIT: Record<string, string> = { minutes: 'мин', hours: 'ч', days: 'дн' };
	const interval = $derived.by(() => {
		const seconds = Number(schedule.interval_seconds ?? 0);
		if (!seconds) return { value: 1, unit: 'hours' };
		if (seconds % 86400 === 0) return { value: seconds / 86400, unit: 'days' };
		if (seconds % 3600 === 0) return { value: seconds / 3600, unit: 'hours' };
		return { value: Math.round(seconds / 60), unit: 'minutes' };
	});
	const period = $derived(
		schedule.cron ? schedule.cron : `каждые ${interval.value} ${UNIT[interval.unit]}`
	);
	const next = $derived(((schedule.schedule?.next ?? []) as string[])[0]);
	const summary = $derived(
		Object.entries(parameters)
			.map(([k, v]) => `${k}=${Array.isArray(v) ? v.join(',') : String(v)}`)
			.join(' ')
	);
</script>

<tr class:off={!schedule.enabled} class:opened={open}>
	<td class="name">
		<span title={schedule.description ?? ''}>{schedule.name ?? schedule.key}</span>
		{#if schedule.sync_pending}
			<span class="pill pill-warn" title="Правка сохранена, в Prefect ещё не доехала">sync</span>
		{/if}
	</td>
	<td class="mono small" title={schedule.timezone ?? ''}>{period}</td>
	<td class="muted small">{schedule.enabled ? (next ? when(next) : 'по интервалу') : '—'}</td>
	<td class="mono small params" title={summary}>{summary || '—'}</td>
	<td class="muted small">{when(schedule.last_run_at)}</td>
	<td class="actions">
		<form method="POST" action="?/run" use:once>
			<input type="hidden" name="id" value={schedule.id} />
			<input type="hidden" name="label" value={schedule.name ?? schedule.key} />
			<button type="submit" class="icon btn-primary" title="Запустить сейчас" disabled={!canRun}>▶</button>
		</form>
		<form method="POST" action="?/toggle" use:once>
			<input type="hidden" name="id" value={schedule.id} />
			<button type="submit" class="icon" title={schedule.enabled ? 'Выключить расписание' : 'Включить расписание'}>
				{schedule.enabled ? '⏸' : '⏵'}
			</button>
		</form>
		<button type="button" class="icon" class:active={open} title="Настроить" onclick={() => (open = !open)}>
			⚙
		</button>
	</td>
</tr>
{#if mine && (form?.error || form?.started || form?.toggled || form?.saved)}
	<tr class="flash-row">
		<td colspan="6">
			{#if form.error}<span class="err">{form.error}</span>
			{:else if form.started}<span class="ok">«{form.label}» поставлен в очередь.</span>
			{:else if form.saved}
				<span class="ok">
					Сохранено{form.synced === false ? ` — Prefect недоступен, досылается позже` : ''}.
				</span>
			{:else}<span class="ok">Готово.</span>{/if}
		</td>
	</tr>
{/if}
{#if open}
	<tr class="editor-row">
		<td colspan="6">
			<div class="editor">
				<form method="POST" action="?/save" use:once class="edit">
					<input type="hidden" name="id" value={schedule.id} />
					<div class="head">
						<b>Расписание</b>
						<label class="radio"><input type="radio" name="mode" value="cron" bind:group={mode} /> cron</label>
						<label class="radio"><input type="radio" name="mode" value="interval" bind:group={mode} /> интервал</label>
						<label class="radio"><input type="checkbox" name="enabled" checked={schedule.enabled} /> включено</label>
					</div>
					{#if mode === 'cron'}
						<div class="line">
							<label>выражение
								<input name="cron" value={schedule.cron ?? '0 3 * * *'} class="mono" placeholder="0 3 * * *" />
							</label>
							<label>пояс <input name="timezone" value={schedule.timezone ?? 'Europe/Moscow'} /></label>
						</div>
					{:else}
						<div class="line">
							<label>каждые <input name="interval_value" type="number" min="1" step="1" value={interval.value} /></label>
							<label>единица
								<select name="interval_unit">
									{#each Object.entries(UNIT) as [value, label]}
										<option {value} selected={interval.unit === value}>{label}</option>
									{/each}
								</select>
							</label>
							<input type="hidden" name="timezone" value={schedule.timezone ?? 'Europe/Moscow'} />
						</div>
					{/if}
					<b>Параметры планового запуска</b>
					<ParamFields {fields} values={parameters} />
					<details class="json">
						<summary class="muted small">JSON для остального</summary>
						<textarea name="parameters_json" rows="2" class="mono" placeholder={'{"ключ": "значение"}'}></textarea>
					</details>
					<div><button type="submit" class="btn-primary">Сохранить</button></div>
				</form>

				<form method="POST" action="?/run" use:once class="edit">
					<input type="hidden" name="id" value={schedule.id} />
					<input type="hidden" name="label" value={`${schedule.name ?? schedule.key} (вручную)`} />
					<div class="head">
						<b>Запустить один раз с другими параметрами</b>
						<span class="muted small">расписание не меняется; пустое поле — из расписания</span>
					</div>
					<ParamFields {fields} values={parameters} />
					<details class="json">
						<summary class="muted small">JSON для остального</summary>
						<textarea name="parameters_json" rows="2" class="mono"></textarea>
					</details>
					<div><button type="submit" disabled={!canRun}>Запустить</button></div>
				</form>
			</div>
		</td>
	</tr>
{/if}

<style>
	.off td:not(.actions) {
		opacity: 0.6;
	}

	.opened td {
		border-bottom-color: transparent;
	}

	.name {
		white-space: nowrap;
	}

	.name .pill {
		margin-left: 6px;
	}

	.small {
		font-size: 12px;
	}

	.params {
		max-width: 320px;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}

	.actions {
		text-align: right;
		white-space: nowrap;
	}

	.actions form {
		display: inline;
	}

	.icon {
		padding: 2px 8px;
		font-size: 12px;
		min-width: 30px;
		margin-left: 4px;
	}

	.icon.active {
		border-color: var(--accent);
		color: var(--accent);
	}

	.flash-row td,
	.editor-row td {
		height: auto;
		padding-top: 6px;
		padding-bottom: 8px;
		font-size: 12.5px;
	}

	.editor {
		display: grid;
		grid-template-columns: 1fr 1fr;
		gap: 12px;
	}

	.edit {
		display: grid;
		gap: 8px;
		padding: 10px 12px;
		border: 1px solid var(--border-soft);
		border-radius: var(--r-card);
		background: var(--bg);
		align-content: start;
	}

	.head {
		display: flex;
		align-items: center;
		gap: 12px;
		flex-wrap: wrap;
	}

	.head b {
		font-weight: 500;
	}

	.radio {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		text-transform: none;
		letter-spacing: 0;
		font-size: 12.5px;
		color: var(--text);
	}

	.line {
		display: grid;
		grid-template-columns: 2fr 1fr;
		gap: 8px;
	}

	.line label {
		font-size: 11px;
		gap: 3px;
	}

	.json summary {
		cursor: pointer;
	}

	.json textarea {
		margin-top: 4px;
	}

	.ok {
		color: var(--good);
	}

	.err {
		color: var(--critical);
	}

	@media (max-width: 900px) {
		.editor {
			grid-template-columns: 1fr;
		}
	}
</style>

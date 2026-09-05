<script lang="ts">
	/**
	 * Одно расписание: когда запускается, с чем, и ручной запуск.
	 *
	 * Всё редактируется на месте: период (cron или интервал), пояс, параметры
	 * запуска по описанию флоу, включённость. Отдельная форма запуска берёт те
	 * же поля — «запустить с другими параметрами» не требует сначала портить
	 * расписание.
	 */
	import { once } from '$lib/once';
	import ParamFields, { type FieldSpec } from '$lib/components/ParamFields.svelte';
	import { when } from '$lib/charts/format';

	let {
		schedule,
		form,
		canRun
	}: { schedule: any; form: any; canRun: boolean } = $props();

	const fields = $derived((schedule.fields ?? []) as FieldSpec[]);
	const parameters = $derived((schedule.parameters ?? {}) as Record<string, unknown>);
	const mine = $derived(form?.id === schedule.id);
	// Начальное значение переключателя — намеренно снимок: дальше им управляет человек.
	// svelte-ignore state_referenced_locally
	let mode = $state<'cron' | 'interval'>(schedule.cron ? 'cron' : 'interval');

	/** Интервал в секундах — в удобную единицу и число. */
	const interval = $derived.by(() => {
		const seconds = Number(schedule.interval_seconds ?? 0);
		if (!seconds) return { value: 1, unit: 'hours' };
		if (seconds % 86400 === 0) return { value: seconds / 86400, unit: 'days' };
		if (seconds % 3600 === 0) return { value: seconds / 3600, unit: 'hours' };
		return { value: Math.round(seconds / 60), unit: 'minutes' };
	});

	const UNIT: Record<string, string> = { minutes: 'мин', hours: 'ч', days: 'дн' };

	const periodText = $derived.by(() => {
		if (schedule.cron) return `cron ${schedule.cron} · ${schedule.timezone ?? ''}`;
		return `каждые ${interval.value} ${UNIT[interval.unit]}`;
	});

	const next = $derived((schedule.schedule?.next ?? []) as string[]);

	/** Параметры одной строкой — что именно уйдёт при плановом запуске. */
	const summary = $derived.by(() => {
		const entries = Object.entries(parameters);
		if (!entries.length) return '';
		return entries
			.map(([key, value]) => `${key}=${Array.isArray(value) ? value.join(',') : String(value)}`)
			.join(' · ');
	});
</script>

<article class="card" class:off={!schedule.enabled}>
	<header>
		<div class="who">
			<h3>{schedule.name ?? schedule.key}</h3>
			<p class="muted small">{schedule.description ?? ''}</p>
		</div>
		<div class="row">
			{#if schedule.sync_pending}
				<span class="pill pill-warn" title="Правка сохранена, но в Prefect ещё не доехала">
					не синхронизировано
				</span>
			{/if}
			<span class="pill {schedule.enabled ? 'pill-good' : 'pill-mute'}">
				{schedule.enabled ? 'включено' : 'выключено'}
			</span>
		</div>
	</header>

	<div class="facts">
		<div>
			<span class="muted small">Период</span>
			<span class="mono">{periodText}</span>
		</div>
		<div>
			<span class="muted small">Ближайшие</span>
			<span class="small">
				{#if !schedule.enabled}
					—
				{:else if next.length}
					{next.slice(0, 2).map(when).join(', ')}
				{:else}
					по интервалу
				{/if}
			</span>
		</div>
		<div>
			<span class="muted small">Параметры</span>
			<span class="mono small">{summary || 'по умолчанию'}</span>
		</div>
		<div>
			<span class="muted small">Последний запуск</span>
			<span class="small">{when(schedule.last_run_at)}</span>
		</div>
	</div>

	<div class="actions">
		<form method="POST" action="?/run" use:once>
			<input type="hidden" name="id" value={schedule.id} />
			<input type="hidden" name="label" value={schedule.name ?? schedule.key} />
			<button type="submit" class="btn-primary" disabled={!canRun}>Запустить сейчас</button>
		</form>
		<form method="POST" action="?/toggle" use:once>
			<input type="hidden" name="id" value={schedule.id} />
			<button type="submit">{schedule.enabled ? 'Выключить' : 'Включить'}</button>
		</form>
		<details class="editor" open={mine}>
			<summary class="btn">Настроить</summary>
			<div class="editor-body">
				<form method="POST" action="?/save" use:once class="edit">
					<input type="hidden" name="id" value={schedule.id} />
					<fieldset>
						<legend>Период</legend>
						<div class="mode">
							<label class="radio">
								<input type="radio" name="mode" value="cron" bind:group={mode} /> cron
							</label>
							<label class="radio">
								<input type="radio" name="mode" value="interval" bind:group={mode} /> интервал
							</label>
						</div>
						{#if mode === 'cron'}
							<div class="line">
								<label>
									Выражение
									<input name="cron" value={schedule.cron ?? '0 3 * * *'} class="mono" />
									<span class="hint">минута час день месяц день-недели — «0 3 * * *» = каждый день в 03:00</span>
								</label>
								<label>
									Пояс
									<input name="timezone" value={schedule.timezone ?? 'Europe/Moscow'} />
								</label>
							</div>
						{:else}
							<div class="line">
								<label>
									Каждые
									<input
										name="interval_value"
										type="number"
										min="1"
										step="1"
										value={interval.value}
									/>
								</label>
								<label>
									Единица
									<select name="interval_unit">
										{#each Object.entries(UNIT) as [value, label]}
											<option {value} selected={interval.unit === value}>{label}</option>
										{/each}
									</select>
								</label>
								<input type="hidden" name="timezone" value={schedule.timezone ?? 'Europe/Moscow'} />
							</div>
						{/if}
						<label class="check">
							<span class="row"><input type="checkbox" name="enabled" checked={schedule.enabled} /> расписание включено</span>
						</label>
					</fieldset>

					<fieldset>
						<legend>Параметры планового запуска</legend>
						<ParamFields {fields} values={parameters} />
						<details class="json">
							<summary class="muted small">JSON для остального</summary>
							<textarea
								name="parameters_json"
								rows="3"
								class="mono"
								placeholder={'{"ключ": "значение"} — дописывается поверх полей выше'}
							></textarea>
						</details>
					</fieldset>
					<div class="row">
						<button type="submit" class="btn-primary">Сохранить расписание</button>
					</div>
				</form>

				<form method="POST" action="?/run" use:once class="edit">
					<input type="hidden" name="id" value={schedule.id} />
					<input type="hidden" name="label" value={`${schedule.name ?? schedule.key} (вручную)`} />
					<fieldset>
						<legend>Запустить один раз с другими параметрами</legend>
						<p class="muted small note">
							Расписание не меняется. Пустые поля берутся из расписания, заполненные — заменяют.
						</p>
						<ParamFields {fields} values={parameters} />
						<details class="json">
							<summary class="muted small">JSON для остального</summary>
							<textarea name="parameters_json" rows="3" class="mono"></textarea>
						</details>
					</fieldset>
					<div class="row">
						<button type="submit" disabled={!canRun}>Запустить с этими параметрами</button>
					</div>
				</form>
			</div>
		</details>
	</div>

	{#if mine && form?.error}
		<p class="flash err" role="alert">{form.error}</p>
	{/if}
	{#if mine && form?.saved}
		<p class="flash ok" role="status">
			Сохранено{form.synced === false ? ` — Prefect недоступен, правка досылается позже (${form.reason ?? ''})` : ' и передано в Prefect'}.
		</p>
	{/if}
	{#if mine && form?.started}
		<p class="flash ok" role="status">«{form.label}» — запуск поставлен в очередь Prefect.</p>
	{/if}
	{#if mine && form?.toggled}
		<p class="flash ok" role="status">Готово.</p>
	{/if}
</article>

<style>
	.card {
		display: grid;
		gap: 10px;
		padding: 14px 16px;
	}

	.off {
		opacity: 0.75;
	}

	header {
		display: flex;
		justify-content: space-between;
		gap: var(--gap);
		align-items: flex-start;
	}

	h3 {
		margin: 0 0 2px;
		font-size: 15px;
	}

	.who p {
		margin: 0;
		max-width: 60ch;
	}

	.facts {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
		gap: 8px 16px;
	}

	.facts div {
		display: grid;
		gap: 2px;
	}

	.small {
		font-size: 12.5px;
	}

	.actions {
		display: flex;
		flex-wrap: wrap;
		gap: 8px;
		align-items: flex-start;
	}

	.actions form {
		margin: 0;
	}

	.editor {
		flex-basis: 100%;
	}

	.editor > summary {
		display: inline-block;
		list-style: none;
		width: auto;
	}

	.editor > summary::-webkit-details-marker {
		display: none;
	}

	.editor[open] > summary {
		border-color: var(--accent);
	}

	.editor-body {
		display: grid;
		gap: 14px;
		margin-top: 12px;
		padding: 14px;
		border: 1px solid var(--border);
		border-radius: var(--r-card);
		background: var(--bg);
	}

	.edit {
		display: grid;
		gap: 10px;
	}

	fieldset {
		border: 1px solid var(--border-soft);
		border-radius: var(--r-card);
		padding: 10px 14px 14px;
		display: grid;
		gap: 10px;
	}

	legend {
		padding: 0 6px;
		font-size: 12px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted);
	}

	.mode {
		display: flex;
		gap: 16px;
	}

	.radio,
	.check .row {
		display: flex;
		align-items: center;
		gap: 6px;
		text-transform: none;
		letter-spacing: 0;
		font-size: 13px;
		color: var(--text);
	}

	.line {
		display: grid;
		grid-template-columns: 2fr 1fr;
		gap: 10px;
	}

	.note {
		margin: 0;
	}

	.json summary {
		cursor: pointer;
	}

	.json textarea {
		margin-top: 6px;
	}

	.flash {
		margin: 0;
		font-size: 13px;
	}

	.ok {
		color: var(--good);
	}

	.err {
		color: var(--critical);
	}

	@media (max-width: 720px) {
		.line {
			grid-template-columns: 1fr;
		}
	}
</style>

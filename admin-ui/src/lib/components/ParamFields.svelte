<script lang="ts">
	/**
	 * Поля параметров флоу — по описанию из каталога (`schedules.fields`).
	 *
	 * Имена полей уходят на сервер с префиксом `p.`: обработчик формы собирает
	 * их в объект параметров, а типы приводит API по тому же описанию. Пустое
	 * поле — «не задано», а не ноль и не пустая строка.
	 */
	export type FieldSpec = {
		key: string;
		label: string;
		type: 'int' | 'float' | 'bool' | 'str' | 'date' | 'list';
		hint?: string;
		default?: unknown;
		choices?: string[];
		minimum?: number | null;
		maximum?: number | null;
	};

	let {
		fields,
		values = {},
		prefix = 'p.'
	}: { fields: FieldSpec[]; values?: Record<string, unknown>; prefix?: string } = $props();

	const text = (value: unknown): string =>
		value === null || value === undefined
			? ''
			: Array.isArray(value)
				? value.join(', ')
				: String(value);

	const placeholder = (field: FieldSpec): string =>
		field.default === null || field.default === undefined ? '' : `по умолчанию ${text(field.default)}`;
</script>

{#if fields.length}
	<div class="fields">
		{#each fields as field}
			{@const name = `${prefix}${field.key}`}
			{@const value = values[field.key]}
			{#if field.type === 'bool'}
				<label class="check">
					<span class="row">
						<input
							type="checkbox"
							name={name}
							value="true"
							checked={value === undefined || value === null
								? Boolean(field.default)
								: Boolean(value)}
						/>
						{field.label}
					</span>
					<!-- Снятый флажок формой не шлётся вовсе; этот маркер говорит
					     обработчику, что поле было и означает false. -->
					<input type="hidden" name={`${prefix}__bool.${field.key}`} value="1" />
					{#if field.hint}<span class="hint">{field.hint}</span>{/if}
				</label>
			{:else if field.choices?.length}
				<label>
					{field.label}
					<select {name}>
						<option value="">{placeholder(field) || 'не задано'}</option>
						{#each field.choices as choice}
							<option value={choice} selected={text(value) === choice}>{choice}</option>
						{/each}
					</select>
					{#if field.hint}<span class="hint">{field.hint}</span>{/if}
				</label>
			{:else}
				<label>
					{field.label}
					<input
						{name}
						type={field.type === 'int' || field.type === 'float'
							? 'number'
							: field.type === 'date'
								? 'date'
								: 'text'}
						step={field.type === 'float' ? 'any' : field.type === 'int' ? '1' : undefined}
						min={field.minimum ?? undefined}
						max={field.maximum ?? undefined}
						value={text(value)}
						placeholder={placeholder(field)}
					/>
					{#if field.hint}<span class="hint">{field.hint}</span>{/if}
				</label>
			{/if}
		{/each}
	</div>
{:else}
	<p class="muted small">У этого флоу нет настраиваемых параметров.</p>
{/if}

<style>
	.fields {
		display: grid;
		grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
		gap: 10px 14px;
	}

	.check .row {
		display: flex;
		align-items: center;
		gap: 8px;
		text-transform: none;
		letter-spacing: 0;
		font-size: 13px;
		color: var(--text);
	}

	.small {
		font-size: 12.5px;
	}
</style>

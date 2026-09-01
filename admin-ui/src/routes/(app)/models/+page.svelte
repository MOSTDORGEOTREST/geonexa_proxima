<script lang="ts">
	import { enhance } from '$app/forms';
	import { n } from '$lib/charts/format';

	let { data, form } = $props();

	const ROLE_LABEL: Record<string, string> = {
		ranker: 'Оценка материала',
		explainer: 'Объяснение релевантности',
		profile_compiler: 'Сборка профиля',
		query_expander: 'Расширение запросов',
		digest_writer: 'Текст дайджеста',
		analyzer: 'Глубокий разбор',
		deep_dive: 'Разбор по запросу',
		chat: 'Диалог в боте'
	};

	const EFFORT = ['', 'none', 'low', 'high', 'max'];
	const usageByRole = $derived(
		Object.fromEntries(data.usage.map((row: any) => [row.bucket, row]))
	);
</script>

<svelte:head><title>Модели · Проксима</title></svelte:head>

<h1>Модели и роли</h1>

<p class="muted note">
	Восемь ролей, у каждой своя модель и свой уровень рассуждения — это и есть раздельная настройка
	лёгких и тяжёлых действий. Модель без поддержки рассуждения не примет уровень выше «none»:
	молча проглотить такую настройку значило бы дать ложную уверенность.
</p>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}

<section class="panel">
	<header><h2>Привязка ролей</h2><span class="muted">за 30 дней</span></header>
	<div class="table-scroll">
		<table>
			<thead>
				<tr>
					<th>Роль</th><th>Модель</th><th>Рассуждение</th><th>Темп.</th>
					<th class="num">Вызовов</th><th class="num">Токенов</th><th class="num">$</th><th></th>
				</tr>
			</thead>
			<tbody>
				{#each data.roles as role}
					<tr>
						<td>
							<div>{ROLE_LABEL[role.role] ?? role.role}</div>
							<div class="muted small mono">{role.role}</div>
						</td>
						<td colspan="3">
							<form method="POST" action="?/bind" use:enhance class="bind">
								<input type="hidden" name="role" value={role.role} />
								<!-- Ручка роли — полная замена, а не правка полей. Всё, чего нет
								     в форме, приходится передавать текущими значениями: иначе
								     сохранение температуры обнуляло запасную модель, потолок
								     токенов и системный промпт роли, и предупреждения об этом
								     не было. -->
								<input type="hidden" name="fallback_model_key" value={role.fallback_model_key ?? ''} />
								<input type="hidden" name="max_tokens" value={role.max_tokens ?? ''} />
								<input
									type="hidden"
									name="system_prompt_override"
									value={role.system_prompt_override ?? ''}
								/>
								<input type="hidden" name="timeout_seconds" value={role.timeout_seconds ?? 120} />
								<input type="hidden" name="concurrency" value={role.concurrency ?? 4} />
								<input type="hidden" name="json_mode" value={role.json_mode === false ? '' : '1'} />
								<input type="hidden" name="enabled" value={role.enabled === false ? '' : '1'} />
								<select name="model_key">
									{#each data.models as model}
										<option value={model.key} selected={model.key === role.model_key}>
											{model.display_name || model.model_name}
										</option>
									{/each}
								</select>
								<select name="reasoning_effort">
									{#each EFFORT as level}
										<option value={level} selected={(role.reasoning_effort ?? '') === level}>
											{level || '—'}
										</option>
									{/each}
								</select>
								<input
									name="temperature"
									type="number"
									step="0.1"
									min="0"
									max="2"
									value={role.temperature ?? 0.2}
								/>
								<button type="submit">Сохранить</button>
							</form>
						</td>
						<td class="num muted">{n(usageByRole[role.role]?.calls)}</td>
						<td class="num muted">{n(usageByRole[role.role]?.tokens)}</td>
						<td class="num muted">
							{usageByRole[role.role]?.cost ? Number(usageByRole[role.role].cost).toFixed(2) : '—'}
						</td>
						<td>
							{#if !role.model_key}
								<span class="pill pill-warn">не назначена</span>
							{/if}
						</td>
					</tr>
				{/each}
			</tbody>
		</table>
	</div>
</section>

<div class="two">
	<section class="panel">
		<header><h2>Провайдеры</h2></header>
		<div class="table-scroll">
			<table>
				<thead><tr><th>Ключ</th><th>Адрес</th><th>Ключ API</th><th>Состояние</th></tr></thead>
				<tbody>
					{#each data.providers as provider}
						<tr>
							<td>{provider.name}</td>
							<td class="mono muted small">{provider.base_url}</td>
							<td class="mono muted small">{provider.api_key ?? '—'}</td>
							<td>
								{#if provider.enabled}
									<span class="pill pill-good">включён</span>
								{:else}
									<span class="pill pill-mute">выключен</span>
								{/if}
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>

	<section class="panel">
		<header><h2>Модели</h2></header>
		<div class="table-scroll">
			<table>
				<thead><tr><th>Ключ</th><th>Модель</th><th>Класс</th><th>Рассуждение</th></tr></thead>
				<tbody>
					{#each data.models as model}
						<tr>
							<td class="mono small">{model.key}</td>
							<td>{model.model_name}</td>
							<td class="muted">{model.tier ?? '—'}</td>
							<td class="muted">{model.supports_reasoning ? model.reasoning_style : 'нет'}</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	</section>
</div>

<style>
	.note {
		max-width: 78ch;
		font-size: 13px;
		margin: 0;
	}

	.small {
		font-size: 12px;
	}

	.bind {
		display: grid;
		grid-template-columns: 1fr 90px 76px auto;
		gap: 6px;
		align-items: center;
	}

	.bind button {
		padding: 5px 12px;
		font-size: 12.5px;
	}

	.two {
		display: grid;
		grid-template-columns: repeat(auto-fit, minmax(380px, 1fr));
		gap: var(--gap);
	}

	.err {
		color: var(--critical);
	}
</style>

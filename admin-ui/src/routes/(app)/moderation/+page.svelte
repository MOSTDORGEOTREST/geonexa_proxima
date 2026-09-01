<script lang="ts">
	import { once } from '$lib/once';
	import { enhance } from '$app/forms';
	import { n, when } from '$lib/charts/format';

	let { data, form } = $props();

	const KIND = { user: 'человек', group: 'группа', channel: 'канал' } as const;

	const items = $derived(data.queue?.items ?? []);
	// `null` — это «API не ответил», и показывать его как «заявок нет» нельзя:
	// неподтверждённый чат снаружи выглядит сломанным ботом, а очередь при этом
	// кажется разобранной.
	const unavailable = $derived(data.queue === null);

	const label = (row: Record<string, unknown>): string =>
		String(row.title || row.username || row.telegram_chat_id);

	const kindOf = (row: Record<string, unknown>): string =>
		KIND[row.kind as keyof typeof KIND] ?? String(row.kind);
</script>

<svelte:head><title>Заявки · Проксима</title></svelte:head>

<div class="spread">
	<h1>Заявки</h1>
	{#if !unavailable}
		<span class="muted">
			{n(items.length)} ждут решения{data.queue?.truncated ? ' (показаны первые)' : ''}
		</span>
	{/if}
</div>

<p class="muted note">
	Проксима работает в группах и каналах. Нажатие <code>/start</code> в личке и добавление бота в
	чат создают заявку, а не подписчика: до подтверждения статус <code>pending</code>, дайджесты
	выключены, диспетчер такую строку не видит. Подтверждение открывает доступ, выдаёт пробную
	подписку и отправляет сообщение в тот же чат.
</p>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}
{#if form?.approval}
	<p class="ok">
		Подтверждено.
		{form.approval.notified ? 'Сообщение отправлено.' : 'Сообщение отправить не удалось.'}
		{#if form.approval.trial_error}Подписка не выдана: {form.approval.trial_error}{/if}
		{#if form.approval.profile_error}Профиль не создан: {form.approval.profile_error}{/if}
	</p>
{/if}
{#if form?.rejected}<p class="ok">Заявка отклонена.</p>{/if}

{#if items.length}
	<div class="queue">
		{#each items as row (row.id)}
			<section class="panel entry">
				<header>
					<div>
						<h2>{label(row)}</h2>
						<p class="muted small">
							{kindOf(row)} · <span class="mono">{row.telegram_chat_id}</span>
							{#if row.username} · @{row.username}{/if}
							{#if row.member_count} · {n(row.member_count)} участников{/if}
						</p>
					</div>
					<span class="muted small">{when(row.first_seen_at)}</span>
				</header>

				<div class="body">
					{#if row.kind === 'channel' && row.can_post_messages === false}
						<!-- Канал без права постить примет подтверждение, но не примет дайджест. -->
						<p class="warn">
							В канале у бота нет права публиковать сообщения — дайджест туда не
							уйдёт. Выдайте право в настройках канала.
						</p>
					{/if}

					<form method="POST" action="?/approve" use:once class="decide">
						<input type="hidden" name="id" value={row.id} />
						<label for="d-{row.id}">
							<span class="label-row">
								Профиль интересов
								{#if row.kind === 'user'}
									<span class="hint">— можно оставить пустым, человек заполнит сам</span>
								{:else}
									<span class="hint">— одна область на строку</span>
								{/if}
							</span>
						</label>
						<textarea
							id="d-{row.id}"
							name="description"
							rows="4"
							placeholder={'Устойчивость откосов и оползневые процессы.\nМониторинг оснований и деформаций сооружений.\nМашинное обучение по данным статического зондирования.'}
						></textarea>
						<p class="muted hint">
							Описание режется на темы по точкам и переводам строк, и каждая ищется
							отдельно: перечисление через запятую в одном предложении останется одной
							темой и усреднится. <a href="/guide">Как писать профиль →</a>
						</p>
						<div class="row">
							<button type="submit" class="btn-primary">Подтвердить</button>
						</div>
					</form>

					<form method="POST" action="?/reject" use:once class="row deny">
						<input type="hidden" name="id" value={row.id} />
						<input name="reason" placeholder="Причина отказа — для аудита" />
						<button type="submit" class="btn-danger">Отклонить</button>
					</form>
				</div>
			</section>
		{/each}
	</div>
{:else if unavailable}
	<section class="panel">
		<p class="empty err">Очередь заявок недоступна: API не отвечает. Это не значит, что заявок нет.</p>
	</section>
{:else}
	<section class="panel"><p class="empty">Нерешённых заявок нет.</p></section>
{/if}

<style>
	.note {
		max-width: 78ch;
		font-size: 13px;
		margin: 0;
	}

	.queue {
		display: grid;
		gap: var(--gap);
	}

	.entry header {
		display: flex;
		align-items: flex-start;
		justify-content: space-between;
		gap: var(--gap);
	}

	.entry h2 {
		font-size: 16px;
		margin: 0;
	}

	.decide {
		display: grid;
		gap: 8px;
	}

	.deny {
		gap: 8px;
	}

	.deny input {
		flex: 1;
	}

	.small {
		font-size: 12.5px;
	}

	.hint {
		margin: 0;
		max-width: 78ch;
		font-size: 12.5px;
	}

	.warn {
		color: var(--warning);
		font-size: 13px;
	}

	.err {
		color: var(--critical);
	}

	.ok {
		color: var(--good);
	}
</style>

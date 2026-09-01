<script lang="ts">
	import { enhance } from '$app/forms';
	import Pill from '$lib/components/Pill.svelte';
	import { n, when } from '$lib/charts/format';

	let { data, form } = $props();

	const STATUS = {
		active: 'подтверждён',
		pending: 'ждёт подтверждения',
		paused: 'пауза',
		blocked: 'заблокирован',
		left: 'ушёл'
	};

	const BOT = {
		creator: 'создатель',
		administrator: 'админ',
		member: 'участник',
		restricted: 'ограничен',
		left: 'вышел',
		kicked: 'удалён'
	};
</script>

<svelte:head><title>Чаты · Проксима</title></svelte:head>

<div class="spread">
	<h1>Группы и каналы</h1>
	<a href="?present={data.present ? '0' : '1'}" class="pill" class:pill-good={data.present}>
		только активные
	</a>
</div>

<p class="muted note">
	Чат заводится заявкой, когда бота в него добавляют: до подтверждения администратором дайджест
	туда не уходит. Раз в шесть часов права сверяются опросом — Telegram сообщает о выходе бота
	ровно один раз, и пропущенный апдейт иначе оставил бы чат «живым» навсегда.
</p>

{#if form?.error}<p class="err" role="alert">{form.error}</p>{/if}
{#if form?.sent}<p class="ok">Сообщение отправлено.</p>{/if}
{#if form?.approval}
	<p class:ok={form.approval.notified} class:err={!form.approval.notified}>
		Чат подтверждён.
		{form.approval.notified ? 'Приветствие отправлено.' : 'Приветствие отправить не удалось.'}
		{#if form.approval.trial_error}Подписка не выдана: {form.approval.trial_error}{/if}
	</p>
{/if}

<section class="panel">
	{#if data.list?.items?.length}
		<div class="table-scroll">
			<table>
				<thead>
					<tr>
						<th>Чат</th><th>Вид</th><th>Статус</th><th>Бот</th><th>Доставка</th>
						<th class="num">Участников</th><th class="num">Профилей</th>
						<th>Проверен</th><th></th>
					</tr>
				</thead>
				<tbody>
					{#each data.list.items as row}
						<tr>
							<td>{row.title || row.username || row.telegram_chat_id}</td>
							<td class="muted">{row.kind === 'channel' ? 'канал' : 'группа'}</td>
							<td><Pill status={row.status} map={STATUS} /></td>
							<td><Pill status={row.bot_status} map={BOT} /></td>
							<td>
								{#if row.can_deliver}
									<span class="pill pill-good">можно слать</span>
								{:else if row.is_present}
									<!-- «Бот в чате» и «боту есть чем писать» — разные вещи. -->
									<span class="pill pill-warn">нет прав постить</span>
								{:else}
									<span class="pill pill-mute">недоступен</span>
								{/if}
							</td>
							<td class="num muted">{n(row.member_count)}</td>
							<td class="num muted">{n(row.profiles)}</td>
							<td class="muted">{when(row.last_checked_at)}</td>
							<td class="actions">
								{#if row.status === 'pending'}
									<form method="POST" action="?/approve" use:enhance>
										<input type="hidden" name="id" value={row.subscriber_id} />
										<button type="submit" class="btn-primary">Подтвердить</button>
									</form>
								{/if}
								<a class="btn" href="/subscribers/{row.subscriber_id}">Профиль</a>
								<form method="POST" action="?/refresh" use:enhance>
									<input type="hidden" name="id" value={row.subscriber_id} />
									<button type="submit">Сверить</button>
								</form>
								<form method="POST" action="?/test" use:enhance>
									<input type="hidden" name="id" value={row.subscriber_id} />
									<button type="submit">Проба</button>
								</form>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		</div>
	{:else if data.list === null}
		<p class="empty err">Список чатов недоступен: API не отвечает.</p>
	{:else}
		<p class="empty">Бота ещё никуда не добавляли.</p>
	{/if}
</section>

<style>
	.note {
		max-width: 78ch;
		font-size: 13px;
		margin: 0;
	}

	.actions {
		display: flex;
		gap: 6px;
		justify-content: flex-end;
	}

	.actions button,
	.actions :global(a.btn) {
		padding: 4px 12px;
		font-size: 12.5px;
	}

	.err {
		color: var(--critical);
	}

	.ok {
		color: var(--good);
	}

	p.err {
		color: var(--critical);
	}

	a.pill {
		text-decoration: none;
	}
</style>

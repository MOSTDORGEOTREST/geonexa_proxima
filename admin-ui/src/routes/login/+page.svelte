<script lang="ts">
	import { enhance } from '$app/forms';
	let { data, form } = $props();
</script>

<svelte:head><title>Вход · Проксима</title></svelte:head>

<main>
	<form method="POST" use:enhance class="card">
		<img src="/brand/gx-emblem.svg" alt="" width="44" height="44" />
		<h1>ПРОКСИМА</h1>
		<p class="sub">Радар научных публикаций ГЕОНЕКСЫ</p>

		<input type="hidden" name="next" value={data.next} />
		<label>
			Логин
			<input name="username" autocomplete="username" value={form?.username ?? ''} required />
		</label>
		<label>
			Пароль
			<input name="password" type="password" autocomplete="current-password" required />
		</label>

		{#if form?.error}
			<p class="error" role="alert">{form.error}</p>
		{/if}

		<button class="btn-primary" type="submit">Войти</button>
	</form>
</main>

<style>
	main {
		min-height: 100dvh;
		display: grid;
		place-items: center;
		padding: 24px;
	}

	form {
		width: min(380px, 100%);
		display: grid;
		gap: 14px;
		justify-items: stretch;
		text-align: center;
		padding: 32px;
	}

	img {
		justify-self: center;
	}

	h1 {
		font-size: 26px;
		letter-spacing: 0.06em;
	}

	.sub {
		margin: -8px 0 8px;
		color: var(--muted);
		font-size: 13px;
	}

	label {
		text-align: left;
	}

	.error {
		margin: 0;
		color: var(--critical);
		font-size: 13px;
	}
</style>

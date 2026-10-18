(function () {
	var relayParams = {};
	window.addEventListener('message', function (event) {
		if (event.origin !== location.origin || !event.data || event.data.action !== 'init') {
			return;
		}
		var params = relayParams[event.data.name];
		if (!params) {
			return;
		}
		event.source.postMessage({action: 'start', params: params}, location.origin);
	});
	function fillSlot(slot, animeId, index) {
		var name = 'xfp_' + index + '_' + Math.random().toString(36).slice(2, 9);
		relayParams[name] = 'mod=player&id=' + encodeURIComponent(animeId) + '&slot=' + index;
		var relay = document.createElement('iframe');
		relay.src = '/engine/ajax/controller.php?mod=xfp&r=3';
		relay.name = name;

		relay.title = slot.getAttribute('data-player-title') || 'Видеоплеер';
		relay.frameBorder = '0';
		relay.allowFullscreen = true;
		relay.setAttribute('allow', 'autoplay; encrypted-media');
		relay.style.width = '100%';
		relay.style.height = '100%';
		slot.appendChild(relay);
	}
	document.addEventListener('DOMContentLoaded', function () {

		Array.prototype.forEach.call(
			document.querySelectorAll('.tabs-block[data-player-anime-id]'),
			function (root) {
				var animeId = root.getAttribute('data-player-anime-id');
				if (!animeId) {
					return;
				}
				Array.prototype.forEach.call(root.querySelectorAll('[data-player-slot]'), function (slot, i) {
					var index = parseInt(slot.getAttribute('data-player-slot'), 10);
					fillSlot(slot, animeId, isNaN(index) ? i : index);
				});
			}
		);
	});
})();

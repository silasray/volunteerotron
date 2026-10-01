(function () {
  "use strict";

  const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  const LENGTH = 25;

  // Cryptographically random, unbiased: bytes that would skew the modulo are
  // discarded (256 isn't a multiple of 62).
  function generatePassword() {
    const limit = 256 - (256 % ALPHABET.length);
    let out = "";
    while (out.length < LENGTH) {
      const bytes = crypto.getRandomValues(new Uint8Array(LENGTH * 2));
      for (const b of bytes) {
        if (b < limit && out.length < LENGTH) out += ALPHABET[b % ALPHABET.length];
      }
    }
    return out;
  }

  document.querySelectorAll(".generate-password").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.target);
      input.value = generatePassword();
      input.focus();
      input.select();
    });
  });

  const picker = document.querySelector(".user-picker select");
  if (picker) picker.addEventListener("change", () => picker.form.submit());

  document.querySelectorAll(".auto-submit").forEach((box) => {
    box.addEventListener("change", () => box.form.requestSubmit()); // so it can be sent in the background
  });

  document.querySelectorAll(".delete-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });
})();

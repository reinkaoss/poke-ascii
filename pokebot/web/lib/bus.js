// Tiny pub/sub shared by the micro-frontends. Events:
//   pokemon:focus {card}     a Pokémon became the subject of the conversation
//   battle:play   {result}   a simulated battle is ready to be shown
//   arena:close              the user left the arena
//   chat:ask      {question} ask the chat something on the user's behalf
//   theme:change  {theme}    scene theme picked in the shell
const target = new EventTarget();

export const bus = {
  emit(type, detail = {}) {
    target.dispatchEvent(new CustomEvent(type, { detail }));
  },
  on(type, handler) {
    const listener = (event) => handler(event.detail);
    target.addEventListener(type, listener);
    return () => target.removeEventListener(type, listener);
  },
};

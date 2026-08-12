(function loadGeneratedData(global) {
    const payload = global.__WTARG_GENERATED_DATA__;
    if (!payload || payload.schemaVersion !== 1) {
        throw new Error('WTARG generated frontend data is missing or incompatible.');
    }
    global.WTARG_DATA = Object.freeze(payload);
    delete global.__WTARG_GENERATED_DATA__;
})(window);

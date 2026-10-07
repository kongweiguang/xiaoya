/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmMap } from '@framework/type/csmmap';
import { csmVector } from '@framework/type/csmvector';
import { CubismLogInfo } from '@framework/utils/cubismdebug';
import { CubismMotionSyncEngineCri } from './cubismmotionsyncenginecri';
import { CubismMotionSyncEngineLib } from './cubismmotionsyncenginelib';
import { CubismMotionSyncEngineVersion } from './cubismmotionsyncengineversion';
import { EngineType } from './cubismmotionsyncutil';
export class CubismMotionSyncEngineController {
    static initializeEngine(engineConfig) {
        let engineLib = new CubismMotionSyncEngineLib();
        const engineName = engineLib.getEngineName();
        const engineType = this.ToEngineType(engineName);
        const nativeVersion = engineLib.getEngineVersion();
        const version = new CubismMotionSyncEngineVersion(nativeVersion);
        if (!this._engineMap) {
            this._engineMap = new csmMap();
        }
        if (this._engineMap.isExist(engineType)) {
            engineLib = void 0;
            engineLib = null;
            return null;
        }
        CubismLogInfo(engineName.s + ' ' + version.toString());
        const isInitialized = engineLib.initializeEngine(engineConfig);
        if (!isInitialized) {
            engineLib = void 0;
            engineLib = null;
            return null;
        }
        let engine = null;
        switch (engineType) {
            case EngineType.EngineType_Cri:
                engine = new CubismMotionSyncEngineCri(engineLib, engineType, engineName, version);
                break;
            default:
                engineLib.disposeEngine();
                engineLib = void 0;
                engineLib = null;
                return null;
        }
        this._engineMap.appendKey(engineType);
        this._engineMap.setValue(engineType, engine);
        return engine;
    }
    static getEngine(type) {
        if (this._engineMap && this._engineMap.isExist(type)) {
            return this._engineMap.getValue(type);
        }
        return null;
    }
    static getEngines() {
        const vector = new csmVector();
        for (let iter = this._engineMap.begin(); iter != this._engineMap.end(); iter.increment()) {
            vector.pushBack(iter.ptr().second);
        }
        return vector;
    }
    static releaseEngineNotForce(engine) {
        this.releaseEngine(engine, false);
    }
    static releaseEngine(engine, isForce) {
        engine.close(isForce);
    }
    static deleteAllEngine() {
        const engines = this.getEngines();
        for (let index = 0; index < engines.getSize(); index++) {
            engines.at(index).close(true);
        }
        this._engineMap.clear();
    }
    static ToEngineType(engineName) {
        let engineType = EngineType.EngineType_Unknown;
        if (engineName.s == 'Live2DCubismMotionSyncEngine_CRI') {
            engineType = EngineType.EngineType_Cri;
        }
        return engineType;
    }
    static deleteAssociation(engine) {
        for (let iter = this._engineMap.begin(); iter != this._engineMap.end(); iter.increment()) {
            if (iter.ptr().first == engine.getType()) {
                engine = void 0;
                this._engineMap.erase(iter);
                break;
            }
        }
    }
}
// Namespace definition for compatibility.
import * as $ from './cubismmotionsyncenginecontroller';
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.CubismMotionSyncEngineController = $.CubismMotionSyncEngineController;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));

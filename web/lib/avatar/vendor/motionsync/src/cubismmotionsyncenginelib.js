/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { csmString } from '@framework/type/csmstring';
import { CubismLogInfo, CubismLogWarning } from '@framework/utils/cubismdebug';
// Namespace definition for compatibility.
import * as $ from './cubismmotionsyncenginelib';
import { EngineType } from './cubismmotionsyncutil';
var ToPointer = Live2DCubismMotionSyncCore.ToPointer;
export class CubismMotionSyncEngineLib {
    constructor() {
        this._analyzeSamplesPtr = 0;
    }
    getEngineVersion() {
        return Live2DCubismMotionSyncCore.CubismMotionSyncEngine.csmMotionSyncGetEngineVersion();
    }
    getEngineName() {
        return new csmString(Live2DCubismMotionSyncCore.CubismMotionSyncEngine.csmMotionSyncGetEngineName());
    }
    initializeEngine(engineConfig) {
        if (this.isInitialized()) {
            CubismLogInfo('Cubism MotionSync Core already initialized.');
            return true;
        }
        this._isEngineInitialized = false;
        const result = Live2DCubismMotionSyncCore.CubismMotionSyncEngine.csmMotionSyncInitializeEngine(engineConfig);
        if (result == Live2DCubismMotionSyncCore.csmMotionSyncFalse) {
            CubismLogWarning('Cubism MotionSync Core Initializing failed.');
            return false;
        }
        this._isEngineInitialized = true;
        return true;
    }
    /** 最后一批输入不会再被下一次分析替换，引擎销毁时必须显式释放。 */
    disposeEngine() {
        if (!this.isInitialized()) {
            CubismLogInfo('Cubism MotionSync Core initialized yet.');
            return;
        }
        ToPointer.Free(this._analyzeSamplesPtr);
        this._analyzeSamplesPtr = 0;
        Live2DCubismMotionSyncCore.CubismMotionSyncEngine.csmMotionSyncDisposeEngine();
        this._isEngineInitialized = false;
    }
    createContext(type, contextConfig, mappingInfoList, mappingInfoListCount) {
        if (!this.isInitialized()) {
            CubismLogInfo('Cubism MotionSync Core initialized yet.');
            return null;
        }
        const context = new Live2DCubismMotionSyncCore.Context();
        // EngineTypeでConfigを分ける
        let contextConfigPtr;
        switch (type) {
            case EngineType.EngineType_Cri:
                {
                    // ポインタへ変換
                    const contextConfigCri = contextConfig;
                    contextConfigCri === null || contextConfigCri === void 0 ? void 0 : contextConfigCri.toNativeArray(true);
                    contextConfigPtr = contextConfigCri === null || contextConfigCri === void 0 ? void 0 : contextConfigCri.getNativePtr();
                }
                break;
            default:
                return null;
        }
        context.csmMotionSyncCreate(contextConfigPtr, mappingInfoList.getMappingInfoListPtr(), mappingInfoListCount);
        return context;
    }
    clearContext(context) {
        if (!this.isInitialized()) {
            CubismLogInfo('Cubism MotionSync Core initialized yet.');
            return;
        }
        context === null || context === void 0 ? void 0 : context.csmMotionSyncClear();
    }
    deleteContext(context) {
        if (!this.isInitialized()) {
            CubismLogInfo('Cubism MotionSync Core initialized yet.');
            return;
        }
        context === null || context === void 0 ? void 0 : context.csmMotionSyncDelete();
    }
    getRequireSampleCount(context) {
        if (!this.isInitialized()) {
            CubismLogInfo('Cubism MotionSync Core initialized yet.');
            return 0;
        }
        if (context == null) {
            CubismLogInfo('context is null.');
            return 0;
        }
        const requireCount = context.csmMotionSyncGetRequireSampleCount();
        return requireCount;
    }
    analyze(context, samples, samplesOffset, sampleCount, analysisResultPtr, analysisConfigPtr) {
        if (!this.isInitialized()) {
            CubismLogInfo('Cubism MotionSync Core initialized yet.');
            return false;
        }
        if (context == null) {
            CubismLogInfo('context is null.');
            return false;
        }
        const analyzeSamples = new Array(sampleCount);
        for (let index = 0; index < sampleCount; index++) {
            analyzeSamples[index] = samples[index + samplesOffset];
        }
        ToPointer.Free(this._analyzeSamplesPtr);
        this._analyzeSamplesPtr = ToPointer.ConvertNumberArrayToFloatArrayPtr(analyzeSamples);
        // samplesの先頭アドレス、Resultのアドレス、configのアドレスを渡す
        const result = context.csmMotionSyncAnalyze(this._analyzeSamplesPtr, sampleCount, analysisResultPtr, analysisConfigPtr);
        return result == Live2DCubismMotionSyncCore.csmMotionSyncTrue ? true : false;
    }
    isInitialized() {
        return this._isEngineInitialized;
    }
}
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.CubismMotionSyncEngineLib = $.CubismMotionSyncEngineLib;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));

/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
var ToPointer = Live2DCubismMotionSyncCore.ToPointer;
const s_contextConfigInfoStructSize = 2;
const s_analysisConfigInfoStructSize = 3;
export class MotionSyncContextConfig_CRI {
    constructor(sampleRate = 0, bitDepth = 0) {
        this.SampleRate = sampleRate;
        this.BitDepth = bitDepth;
    }
    toNativeArray(forceConversion) {
        // 強制的に新規作成しないのであれば早期リターン
        if (!forceConversion && this._nativeArray) {
            return;
        }
        if (this._nativeArray) {
            this.releaseNativeArray();
        }
        this._nativeArray = new Int32Array(s_contextConfigInfoStructSize);
        this._nativeArrayPtr = ToPointer.Malloc(this._nativeArray.length * this._nativeArray.BYTES_PER_ELEMENT);
        // Nativeポインタへの変換
        this._nativeArray = ToPointer.ConvertContextConfigCriToInt32Array(this._nativeArray, this._nativeArrayPtr, this.SampleRate, this.BitDepth);
    }
    getNativePtr() {
        return this._nativeArrayPtr;
    }
    releaseNativeArray() {
        this.deallocNativeArrayPtr();
        this._nativeArray = void 0;
    }
    deallocNativeArrayPtr() {
        // 配列本体を解放
        ToPointer.Free(this._nativeArrayPtr);
        this._nativeArrayPtr = 0;
    }
}
export class MotionSyncAnalysisConfig_CRI {
    constructor(blendRatio = 0.0, smoothing = 0, audioLevelEffectRatio = 0.0) {
        this.BlendRatio = blendRatio;
        this.Smoothing = smoothing;
        this.AudioLevelEffectRatio = audioLevelEffectRatio;
    }
    toNativeArray(forceConversion) {
        // 強制的に新規作成しないのであれば既にあるものを返す
        if (!forceConversion && this._nativeArray) {
            return this._nativeArray;
        }
        if (this._nativeArray) {
            this.releaseNativeArray();
        }
        this._nativeArray = new Float32Array(s_analysisConfigInfoStructSize);
        this._nativeArrayPtr = ToPointer.Malloc(this._nativeArray.length * this._nativeArray.BYTES_PER_ELEMENT);
        // Nativeポインタへの変換
        this._nativeArray = ToPointer.ConvertAnalysisConfigToFloat32Array(this._nativeArray, this._nativeArrayPtr, this.BlendRatio, this.Smoothing, this.AudioLevelEffectRatio);
        return this._nativeArray;
    }
    releaseNativeArray() {
        this.deallocNativePtr();
        this._nativeArray = void 0;
    }
    deallocNativePtr() {
        // 配列本体を解放
        ToPointer.Free(this._nativeArrayPtr);
        this._nativeArrayPtr = 0;
    }
}
// Namespace definition for compatibility.
import * as $ from './motionsyncconfig_cri';
// eslint-disable-next-line @typescript-eslint/no-namespace
export var Live2DCubismMotionSyncFramework;
(function (Live2DCubismMotionSyncFramework) {
    Live2DCubismMotionSyncFramework.MotionSyncContextConfig_CRI = $.MotionSyncContextConfig_CRI;
})(Live2DCubismMotionSyncFramework || (Live2DCubismMotionSyncFramework = {}));

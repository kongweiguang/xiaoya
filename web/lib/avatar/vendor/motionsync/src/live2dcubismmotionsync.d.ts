/**
 * Copyright(c) Live2D Inc. All rights reserved.
 *
 * Use of this source code is governed by the Live2D Open Software license
 * that can be found at https://www.live2d.com/eula/live2d-open-software-license-agreement_en.html.
 */
import { LogLevel } from '@framework/live2dcubismframework';
import { CubismModel } from '@framework/model/cubismmodel';
import { csmVector } from '@framework/type/csmvector';
import { CubismMotionSyncData, CubismMotionSyncDataSetting } from './cubismmotionsyncdata';
import { CubismMotionSyncEngineAnalysisResult } from './cubismmotionsyncengineanalysisresult';
import { ICubismMotionSyncProcessor } from './icubismmotionsyncprocessor';
export declare class CubismMotionSync {
    /**
     * Cubism MotionSync FrameworkのAPIを使用可能にする。
     *  APIを実行する前に必ずこの関数を実行すること。
     *  一度準備が完了して以降は、再び実行しても内部処理がスキップされます。
     *
     * @param    option      MotionSyncLogOptionクラスのインスタンス
     *
     * @return   準備処理が完了したらtrueが返ります。
     */
    static startUp(option?: MotionSyncOption): boolean;
    /**
     * StartUp()で初期化したCubism MotionSync Frameworkの各パラメータをクリアします。
     * Dispose()したCubism MotionSync Frameworkを再利用する際に利用してください。
     */
    static cleanUp(): void;
    /**
     * Cubism MotionSync Framework内のリソースを初期化してモデルを表示可能な状態にします。
     *     再度Initialize()するには先にDispose()を実行する必要があります。
     */
    static initialize(): void;
    /**
     * Cubism MotionSync Framework内の全てのリソースを解放します。
     *      ただし、外部で確保されたリソースについては解放しません。
     *      外部で適切に破棄する必要があります。
     */
    static dispose(): void;
    /**
     * Cubism MotionSync FrameworkのAPIを使用する準備が完了したかどうか
     * @return APIを使用する準備が完了していればtrueが返ります。
     */
    static isStarted(): boolean;
    /**
     * Cubism MotionSync Frameworkのリソース初期化がすでに行われているかどうか
     * @return リソース確保が完了していればtrueが返ります
     */
    static isInitialized(): boolean;
    static create(model: CubismModel, buffer: ArrayBuffer, size: number, samplePerSec: number): CubismMotionSync;
    private static InitializeEngineCri;
    static delete(instance: CubismMotionSync): void;
    setSoundBuffer(processIndex: number, buffer: csmVector<number>, startIndex: number): void;
    release(): void;
    updateParameters(model: CubismModel, deltaTimeSeconds: number): void;
    private analyze;
    setBlendRatio(processIndex: number, blendRatio: number): void;
    SetSmoothing(processIndex: number, smoothing: number): void;
    SetSampleRate(processIndex: number, sampleRate: number): void;
    getData(): CubismMotionSyncData;
    getLastTotalProcessedCount(processIndex: number): number;
    private constructor();
    private _processorInfoList;
    private _data;
}
export declare class MotionSyncOption {
    engineConfig: MotionSyncEngineConfigCri;
    logFunction: Live2DCubismMotionSyncCore.csmMotionSyncLogFunction;
    loggingLevel: LogLevel;
}
export declare class MotionSyncEngineConfigCriData {
    engineConfigBuffer: Int32Array;
    engineConfigPtr: number;
}
export declare class CubismProcessorInfo {
    constructor(processor: ICubismMotionSyncProcessor, model: CubismModel, setting: CubismMotionSyncDataSetting);
    init(setting: CubismMotionSyncDataSetting): void;
    _processor: ICubismMotionSyncProcessor;
    _blendRatio: number;
    _smoothing: number;
    _sampleRate: number;
    _audioLevelEffectRatio: number;
    _sampleBuffer: csmVector<number>;
    _sampleBufferIndex: number;
    _model: CubismModel;
    _analysisResult: CubismMotionSyncEngineAnalysisResult;
    _currentRemainTime: number;
    _lastSmoothedList: csmVector<number>;
    _lastDampedList: csmVector<number>;
    _lastTotalProcessedCount: number;
}
import * as $ from './live2dcubismmotionsync';
import { MotionSyncEngineConfig_CRI as MotionSyncEngineConfigCri } from './motionsyncconfig_cri';
export declare namespace Live2DCubismMotionSyncFramework {
    const CubismMotionSync: typeof $.CubismMotionSync;
    type CubismMotionSync = $.CubismMotionSync;
}
